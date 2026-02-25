#!/usr/bin/env python3
"""
Git-aware Code Duplication Checker

Checks if newly modified/added code duplicates existing code in the repository.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Optional, Tuple


@dataclass
class CodeBlock:
    """Represents a block of code."""
    file_path: str
    start_line: int
    end_line: int
    content: str
    normalized_content: str
    is_new: bool  # True if from modified file, False if from existing code


@dataclass
class Duplicate:
    """Represents a duplicate between new and existing code."""
    new_block: CodeBlock
    existing_block: CodeBlock
    similarity: float
    suggestion: str


LANGUAGE_CONFIG = {
    'java': {
        'extensions': ['.java'],
        'single_comment': '//',
        'multi_comment_start': '/*',
        'multi_comment_end': '*/'
    },
    'javascript': {
        'extensions': ['.js', '.jsx'],
        'single_comment': '//',
        'multi_comment_start': '/*',
        'multi_comment_end': '*/'
    },
    'typescript': {
        'extensions': ['.ts', '.tsx'],
        'single_comment': '//',
        'multi_comment_start': '/*',
        'multi_comment_end': '*/'
    },
    'python': {
        'extensions': ['.py'],
        'single_comment': '#',
        'multi_comment_start': '"""',
        'multi_comment_end': '"""'
    },
    'go': {
        'extensions': ['.go'],
        'single_comment': '//',
        'multi_comment_start': '/*',
        'multi_comment_end': '*/'
    },
    'c': {
        'extensions': ['.c', '.h'],
        'single_comment': '//',
        'multi_comment_start': '/*',
        'multi_comment_end': '*/'
    },
    'cpp': {
        'extensions': ['.cpp', '.hpp', '.cc', '.hh'],
        'single_comment': '//',
        'multi_comment_start': '/*',
        'multi_comment_end': '*/'
    },
    'csharp': {
        'extensions': ['.cs'],
        'single_comment': '//',
        'multi_comment_start': '/*',
        'multi_comment_end': '*/'
    },
    'ruby': {
        'extensions': ['.rb'],
        'single_comment': '#',
        'multi_comment_start': '=begin',
        'multi_comment_end': '=end'
    }
}


def run_git_command(args: List[str], cwd: str = None) -> str:
    """Run a git command and return output."""
    try:
        result = subprocess.run(
            ['git'] + args,
            capture_output=True,
            cwd=cwd or os.getcwd()
        )
        return result.stdout.decode('utf-8', errors='ignore').strip()
    except Exception as e:
        print(f"Error running git command: {e}")
        return ""


def get_git_root() -> str:
    """Get the git repository root directory."""
    return run_git_command(['rev-parse', '--show-toplevel'])


def get_modified_files(staged_only: bool = False) -> List[Tuple[str, str]]:
    """
    Get list of modified files from git.
    Returns list of (status, filepath) tuples.
    Status: M=modified, A=added, R=renamed
    """
    files = []

    if staged_only:
        # Staged files only
        output = run_git_command(['diff', '--cached', '--name-status', '--diff-filter=AMR'])
    else:
        # All uncommitted changes
        output = run_git_command(['diff', '--name-status', '--diff-filter=AMR'])
        # Also get staged files
        staged_output = run_git_command(['diff', '--cached', '--name-status', '--diff-filter=AMR'])
        if staged_output:
            output = output + '\n' + staged_output if output else staged_output

    for line in output.strip().split('\n'):
        if not line:
            continue
        parts = line.split('\t')
        if len(parts) >= 2:
            status = parts[0][0]  # M, A, R
            filepath = parts[-1]  # For renames, last part is new path
            files.append((status, filepath))

    return list(set(files))  # Remove duplicates


def get_diff_for_file(filepath: str, staged_only: bool = False) -> str:
    """Get the diff for a specific file."""
    if staged_only:
        return run_git_command(['diff', '--cached', filepath])
    return run_git_command(['diff', filepath])


def detect_language(file_path: str) -> str:
    """Detect programming language from file extension."""
    ext = Path(file_path).suffix.lower()
    for lang, config in LANGUAGE_CONFIG.items():
        if ext in config['extensions']:
            return lang
    return 'unknown'


def remove_comments(content: str, lang: str) -> str:
    """Remove comments from code."""
    if lang not in LANGUAGE_CONFIG:
        return content

    config = LANGUAGE_CONFIG[lang]
    lines = content.split('\n')
    result = []
    in_multiline = False

    for line in lines:
        if config['multi_comment_start'] in line:
            if config['multi_comment_end'] in line:
                start = line.find(config['multi_comment_start'])
                end = line.find(config['multi_comment_end'], start + len(config['multi_comment_start']))
                if end != -1:
                    line = line[:start] + line[end + len(config['multi_comment_end']):]
            else:
                in_multiline = True
                line = line[:line.find(config['multi_comment_start'])]

        if in_multiline:
            if config['multi_comment_end'] in line:
                in_multiline = False
                line = line[line.find(config['multi_comment_end']) + len(config['multi_comment_end']):]
            else:
                continue

        if config['single_comment'] in line:
            line = line[:line.find(config['single_comment'])]

        result.append(line)

    return '\n'.join(result)


def normalize_code(content: str) -> str:
    """Normalize code for comparison."""
    # Remove extra whitespace
    content = re.sub(r'\s+', ' ', content)
    # Normalize variable names
    content = re.sub(r'\b[a-zA-Z_]\w*\b', 'VAR', content)
    # Normalize string literals
    content = re.sub(r'"[^"]*"', '"STR"', content)
    content = re.sub(r"'[^']*'", "'STR'", content)
    # Normalize numbers
    content = re.sub(r'\b\d+\b', 'NUM', content)
    return content.strip().lower()


def parse_diff_hunks(diff_text: str) -> List[Tuple[int, str]]:
    """
    Parse diff text and extract added/changed hunks with their line numbers.
    Returns list of (start_line, content) tuples.
    """
    hunks = []
    current_line = 0
    current_content = []

    for line in diff_text.split('\n'):
        if line.startswith('@@'):
            # Save previous hunk if exists
            if current_content:
                content = '\n'.join(current_content)
                if len(content) > 50:  # Skip very short hunks
                    hunks.append((current_line, content))
                current_content = []

            # Parse new hunk header: @@ -old_start,old_count +new_start,new_count @@
            match = re.search(r'\+\d+(?:,\d+)?', line)
            if match:
                current_line = int(match.group().replace('+', '').split(',')[0])

        elif line.startswith('+') and not line.startswith('+++'):
            # Added line
            content = line[1:]
            if content.strip():
                current_content.append(content)

        elif line.startswith('-') and not line.startswith('---'):
            # Removed line - skip
            pass

        elif line.startswith(' '):
            # Context line - save current hunk if we have content
            if current_content:
                content = '\n'.join(current_content)
                if len(content) > 50:
                    hunks.append((current_line, content))
                current_content = []

    # Save last hunk
    if current_content:
        content = '\n'.join(current_content)
        if len(content) > 50:
            hunks.append((current_line, content))

    return hunks


def extract_code_blocks_from_hunks(file_path: str, hunks: List[Tuple[int, str]], min_lines: int = 5) -> List[CodeBlock]:
    """Extract code blocks from diff hunks."""
    lang = detect_language(file_path)
    if lang == 'unknown':
        return []

    blocks = []
    for start_line, content in hunks:
        lines = content.split('\n')

        # Skip if too short
        if len(lines) < min_lines:
            continue

        # Create sliding windows for larger hunks
        for i in range(0, len(lines) - min_lines + 1, min_lines):
            chunk_lines = lines[i:i + min_lines * 2]  # Use larger chunks
            chunk_content = '\n'.join(chunk_lines)

            # Remove comments and normalize
            clean_content = remove_comments(chunk_content, lang)
            normalized = normalize_code(clean_content)

            if len(normalized) < 30:
                continue

            blocks.append(CodeBlock(
                file_path=file_path,
                start_line=start_line + i,
                end_line=start_line + i + len(chunk_lines),
                content=chunk_content,
                normalized_content=normalized,
                is_new=True
            ))

    return blocks


def extract_all_code_blocks(directory: str, min_lines: int = 5) -> List[CodeBlock]:
    """Extract code blocks from all existing files."""
    all_blocks = []

    ignore_patterns = ['node_modules', 'vendor', '.git', 'target', 'build', 'dist',
                       '__pycache__', '.idea', '.vscode']

    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in ignore_patterns]

        for file in files:
            file_path = os.path.join(root, file)

            # Skip non-code files
            lang = detect_language(file_path)
            if lang == 'unknown':
                continue

            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
            except Exception:
                continue

            lines = content.split('\n')
            if len(lines) < min_lines:
                continue

            # Extract sliding window blocks
            for i in range(0, len(lines) - min_lines + 1, min_lines):
                block_lines = lines[i:i + min_lines * 2]
                block_content = '\n'.join(block_lines)

                clean_content = remove_comments(block_content, lang)
                normalized = normalize_code(clean_content)

                if len(normalized) < 30:
                    continue

                all_blocks.append(CodeBlock(
                    file_path=file_path,
                    start_line=i + 1,
                    end_line=i + len(block_lines),
                    content=block_content,
                    normalized_content=normalized,
                    is_new=False
                ))

    return all_blocks


def calculate_similarity(block1: str, block2: str) -> float:
    """Calculate similarity between two code blocks using Jaccard."""
    tokens1 = set(block1.split())
    tokens2 = set(block2.split())

    if not tokens1 or not tokens2:
        return 0.0

    intersection = tokens1 & tokens2
    union = tokens1 | tokens2

    return len(intersection) / len(union)


def generate_suggestion(new_block: CodeBlock, existing_block: CodeBlock) -> str:
    """Generate refactoring suggestion based on duplicate context."""
    # Same file
    if new_block.file_path == existing_block.file_path:
        return "Extract method to reduce duplication within this file"

    # Different files
    lang = detect_language(new_block.file_path)

    if lang in ['java', 'csharp', 'cpp']:
        # Check if in same package/directory
        new_dir = os.path.dirname(new_block.file_path)
        existing_dir = os.path.dirname(existing_block.file_path)

        if new_dir == existing_dir:
            return "Extract shared method to a utility class in the same package"
        else:
            return "Consider calling existing method or extracting to common base class"

    elif lang in ['javascript', 'typescript']:
        return "Import and reuse the existing function, or extract to a shared utility module"

    elif lang == 'python':
        return "Import and reuse the existing function, or extract to a shared module"

    else:
        return "Extract shared code to a common module"


def find_duplicates(new_blocks: List[CodeBlock], existing_blocks: List[CodeBlock],
                    similarity_threshold: float) -> List[Duplicate]:
    """Find duplicates between new code and existing code."""
    duplicates = []

    for new_block in new_blocks:
        for existing_block in existing_blocks:
            # Skip comparing with itself (same file, same lines)
            if new_block.file_path == existing_block.file_path:
                if abs(new_block.start_line - existing_block.start_line) < 10:
                    continue

            similarity = calculate_similarity(
                new_block.normalized_content,
                existing_block.normalized_content
            )

            if similarity >= similarity_threshold:
                duplicates.append(Duplicate(
                    new_block=new_block,
                    existing_block=existing_block,
                    similarity=similarity * 100,
                    suggestion=generate_suggestion(new_block, existing_block)
                ))

    # Sort by similarity (highest first) and remove duplicates
    duplicates.sort(key=lambda d: d.similarity, reverse=True)

    # Remove duplicate findings (same new block location)
    seen = set()
    unique_duplicates = []
    for dup in duplicates:
        key = (dup.new_block.file_path, dup.new_block.start_line)
        if key not in seen:
            seen.add(key)
            unique_duplicates.append(dup)

    return unique_duplicates


def generate_report(duplicates: List[Duplicate], modified_files: List[str],
                    format: str = 'text') -> str:
    """Generate duplication report."""
    if format == 'json':
        return generate_json_report(duplicates, modified_files)
    return generate_text_report(duplicates, modified_files)


def generate_text_report(duplicates: List[Duplicate], modified_files: List[str]) -> str:
    """Generate text format report."""
    lines = [
        "=" * 60,
        "GIT DUPLICATE DETECTION REPORT",
        "=" * 60,
        f"\nModified files checked: {len(modified_files)}",
        f"Duplicates found: {len(duplicates)}",
        ""
    ]

    if not duplicates:
        lines.append("No duplicates found! Your new code looks clean.")
        return '\n'.join(lines)

    for i, dup in enumerate(duplicates, 1):
        lines.extend([
            f"[DUPLICATE {i}] Similarity: {dup.similarity:.1f}%",
            f"  New code:     {dup.new_block.file_path}:{dup.new_block.start_line}-{dup.new_block.end_line}",
            f"  Existing:     {dup.existing_block.file_path}:{dup.existing_block.start_line}-{dup.existing_block.end_line}",
            f"  Suggestion:   {dup.suggestion}",
            "",
            "  New code preview (first 5 lines):",
        ])

        # Show preview of new code
        preview_lines = dup.new_block.content.split('\n')[:5]
        for line in preview_lines:
            lines.append(f"    {line}")
        lines.append("")

    return '\n'.join(lines)


def generate_json_report(duplicates: List[Duplicate], modified_files: List[str]) -> str:
    """Generate JSON format report."""
    report = {
        'summary': {
            'modified_files': len(modified_files),
            'duplicates_found': len(duplicates)
        },
        'modified_files': modified_files,
        'duplicates': [
            {
                'similarity': dup.similarity,
                'suggestion': dup.suggestion,
                'new_code': {
                    'file': dup.new_block.file_path,
                    'start_line': dup.new_block.start_line,
                    'end_line': dup.new_block.end_line,
                    'content': dup.new_block.content[:200] + '...' if len(dup.new_block.content) > 200 else dup.new_block.content
                },
                'existing_code': {
                    'file': dup.existing_block.file_path,
                    'start_line': dup.existing_block.start_line,
                    'end_line': dup.existing_block.end_line
                }
            }
            for dup in duplicates
        ]
    }
    return json.dumps(report, indent=2)


def main():
    parser = argparse.ArgumentParser(
        description='Check git modified files for code duplication'
    )
    parser.add_argument(
        '--similarity', '-s',
        type=float,
        default=75.0,
        help='Minimum similarity percentage (default: 75)'
    )
    parser.add_argument(
        '--min-lines', '-l',
        type=int,
        default=5,
        help='Minimum lines to consider (default: 5)'
    )
    parser.add_argument(
        '--staged',
        action='store_true',
        help='Check staged files only'
    )
    parser.add_argument(
        '--format', '-f',
        choices=['text', 'json'],
        default='text',
        help='Output format (default: text)'
    )
    parser.add_argument(
        '--output', '-o',
        help='Output file (default: stdout)'
    )

    args = parser.parse_args()

    # Get git root
    git_root = get_git_root()
    if not git_root:
        print("Error: Not a git repository")
        return 1

    print(f"Git repository: {git_root}")

    # Get modified files
    modified = get_modified_files(staged_only=args.staged)
    if not modified:
        print("No modified files found.")
        return 0

    print(f"Found {len(modified)} modified file(s)")

    # Extract new code blocks from modified files
    new_blocks = []
    modified_file_paths = []

    for status, filepath in modified:
        full_path = os.path.join(git_root, filepath)
        if not os.path.exists(full_path):
            continue

        modified_file_paths.append(filepath)
        lang = detect_language(filepath)
        if lang == 'unknown':
            continue

        if status == 'A':
            # New file - extract all code
            try:
                with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                diff_text = f"@@ -0,0 +1,{len(content.split(chr(10)))} @@\n" + ''.join(f'+{line}\n' for line in content.split('\n'))
            except Exception as e:
                print(f"Warning: Could not read {filepath}: {e}")
                continue
        else:
            # Modified file - get diff
            diff_text = get_diff_for_file(filepath, staged_only=args.staged)

        if not diff_text:
            continue

        hunks = parse_diff_hunks(diff_text)
        blocks = extract_code_blocks_from_hunks(filepath, hunks, min_lines=args.min_lines)
        new_blocks.extend(blocks)

    print(f"Extracted {len(new_blocks)} code blocks from changes")

    if not new_blocks:
        print("No significant code changes found to analyze.")
        return 0

    # Extract existing code blocks from all files
    print("Scanning existing codebase for duplicates...")
    existing_blocks = extract_all_code_blocks(git_root, min_lines=args.min_lines)

    # Filter out blocks from modified files (we only want to compare against unchanged files)
    modified_set = set(modified_file_paths)
    existing_blocks = [b for b in existing_blocks if b.file_path not in modified_set]

    print(f"Comparing against {len(existing_blocks)} existing code blocks")

    # Find duplicates
    duplicates = find_duplicates(
        new_blocks,
        existing_blocks,
        args.similarity / 100.0
    )

    # Generate report
    report = generate_report(duplicates, modified_file_paths, format=args.format)

    if args.output:
        with open(args.output, 'w') as f:
            f.write(report)
        print(f"\nReport saved to {args.output}")
    else:
        print(report)

    return 0 if not duplicates else 1


if __name__ == '__main__':
    sys.exit(main())
