---
name: d2-diagrams
description: Create D2 diagrams from natural language descriptions. Use when user wants to create diagrams, flowcharts, architecture diagrams, ERDs, UML class diagrams, sequence diagrams, or mentions D2, diagramming, or text-to-diagram tools like Mermaid or Graphviz.
---

# D2 Diagrams

D2 is a modern declarative diagramming language.

## Critical Syntax Rules

### Style Syntax - Avoid "missing value after colon" Error

**Always use style blocks for hyphenated properties** (stroke-width, stroke-dash, border-radius, font-size, etc.). Dot notation does NOT support hyphens.

**WRONG - This causes errors:**
```d2
node.style.stroke-width: 2        # ERROR
node.style.stroke-dash: 3          # ERROR
node.style.border-radius: 5        # ERROR
```

**CORRECT - Use style blocks:**
```d2
node.style: {
  stroke-width: 2
  stroke-dash: 3
  border-radius: 5
}
```

**Dot notation only works for non-hyphenated properties:**
```d2
node.style.fill: red               # OK - no hyphen
node.style.stroke: blue            # OK - no hyphen
node.shape: circle                 # OK
```

### Valid Ranges for Numeric Properties

| Property | Valid Range |
|----------|-------------|
| stroke-width | 1-15 |
| stroke-dash | 0-10 |
| border-radius | 0-20 |
| font-size | 8-100 |
| opacity | 0-100 |

## Quick Syntax Reference

### Basic Shapes
```d2
A                  # Default rectangle
A: circle
B: diamond
C: cylinder
D: database
E: queue
```

### Connections
```d2
A -- B           # Line
A -> B           # Arrow
A <- B           # Reverse arrow
A <-> B          # Double arrow
A -> B: label    # Labeled connection
```

### Labels
```d2
A: My Label
A -> B: connects to
```

### Containers
```d2
container: {
  A
  B
}

container "My Group": {
  A
  B
  subgroup: {
    C
  }
}
```

## Common Shapes

| Shape | Syntax |
|-------|--------|
| Rectangle | `shape: rectangle` or default |
| Circle | `shape: circle` |
| Oval | `shape: oval` |
| Diamond | `shape: diamond` |
| Cylinder | `shape: cylinder` |
| Database | `shape: database` |
| Queue | `shape: queue` |
| Person | `shape: person` |
| Class | `shape: class` |
| SQL Table | `shape: sql_table` |

## Styling Reference

### Style Block (Preferred for Multiple Properties)
```d2
node.style: {
  fill: #e1f5fe
  stroke: #0288d1
  stroke-width: 2
  stroke-dash: 3
  border-radius: 4
  opacity: 80
}
```

### Dot Notation (Single Non-Hyphenated Properties Only)
```d2
node.shape: circle
node.style.fill: red
node.style.stroke: blue
node.style.font-color: white
```

### Colors
- Named: `red`, `blue`, `lightblue`
- Hex: `#ff0000`, `#f00`
- RGB: `rgb(255, 0, 0)`

See STYLES.md for complete reference.

## Classes

Define in `classes: {}` block, apply with semicolons for multiple:

```d2
classes: {
  important: {
    style: {
      fill: red
      stroke: blue
      stroke-width: 2
    }
  }
}

importantNode.class: important
node.class: [important; another]  # Use semicolons!
```

See CLASSES_REUSE.md for details.

## Diagram Patterns

### Flowchart
```d2
direction: down

Start: {
  shape: oval
  style: { fill: #c8e6c9; stroke: #2e7d32; stroke-width: 2 }
}

Process: {
  shape: rectangle
  style: { fill: #e3f2fd; stroke: #1565c0; stroke-width: 2 }
}

Decision: {
  shape: diamond
  style: { fill: #fff9c4; stroke: #f9a825; stroke-width: 2 }
}

End: {
  shape: oval
  style: { fill: #ffcdd2; stroke: #c62828; stroke-width: 2 }
}

Start -> Process
Process -> Decision: yes
Decision -> End: no
```

### Architecture Diagram
```d2
direction: right

Frontend: {
  style: { fill: #e3f2fd; stroke: #1976d2 }
}

Backend: {
  style: { fill: #f3e5f5; stroke: #7b1fa2 }
}

Database: {
  shape: database
  style: { fill: #fff3e0; stroke: #ef6c00 }
}

Frontend -> Backend
Backend -> Database
```

### Sequence Diagram
```d2
A -> B: request
B -> C: forward
C -> A: response
```

## Advanced Features

### Icons
```d2
logo.icon: https://example.com/logo.png
logo.icon: https://example.com/logo.png { width: 100 }
```

### Code Blocks
```d2
code: {
  code_block: |
    def hello():
        print("world")
}
```

### Dashed Connections
```d2
# Using syntax sugar
A -.-> B

# Using style
A -> B: {
  style: {
    stroke-dash: 5
  }
}
```

See EXAMPLES.md for more patterns.

## Additional References

- STYLES.md - Complete style properties reference
- TROUBLESHOOTING.md - Common errors and solutions
- EXAMPLES.md - Common diagram patterns
- CLASSES.md - UML class diagram syntax
- SQL_TABLES.md - Database table syntax
