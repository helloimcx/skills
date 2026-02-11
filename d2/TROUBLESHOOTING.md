# D2 Troubleshooting Guide

## Error: "missing value after colon"

This is the most common D2 error. The cause is always using hyphenated properties with dot notation.

### The Problem

D2 does NOT support hyphenated properties (stroke-width, stroke-dash, border-radius, font-size, etc.) in dot notation.

### Wrong vs Correct

**WRONG:**
```d2
node.style.stroke-width: 2     # ERROR
node.style.stroke-dash: 3       # ERROR
node.style.border-radius: 5     # ERROR
node.style.font-size: 14        # ERROR
connection.style.stroke-dash: 5 # ERROR
```

**CORRECT - Use style blocks:**
```d2
node.style: {
  stroke-width: 2
  stroke-dash: 3
  border-radius: 5
}
```

**CORRECT - Dot notation for non-hyphenated only:**
```d2
node.style.fill: red            # OK - no hyphen
node.style.stroke: blue         # OK - no hyphen
node.style.font-color: white    # OK - no hyphen
node.shape: circle              # OK - no hyphen
```

## Invalid Value Ranges

All numeric style properties have enforced ranges.

| Property | Valid Range | Wrong Example | Correct Example |
|----------|-------------|---------------|-----------------|
| stroke-width | 1-15 | `stroke-width: 20` | `stroke-width: 3` |
| stroke-dash | 0-10 | `stroke-dash: 15` | `stroke-dash: 5` |
| border-radius | 0-20 | `border-radius: 50` | `border-radius: 10` |
| font-size | 8-100 | `font-size: 5` | `font-size: 12` |
| opacity | 0-100 | `opacity: 150` | `opacity: 80` |

## Classes Not Applying

### Define classes in `classes: {}` block

**WRONG:**
```d2
myClass.style: { fill: red }   # Won't work as a class
node.class: myClass            # Error: class not found
```

**CORRECT:**
```d2
classes: {
  myClass: {
    style: { fill: red }
  }
}
node.class: myClass            # Works!
```

### Use semicolons for multiple classes

**WRONG:**
```d2
node.class: [class1, class2]   # Uses commas
```

**CORRECT:**
```d2
node.class: [class1; class2]   # Uses semicolons
```

## Syntax Checklist

Before rendering, verify:

### Style Properties
- [ ] Hyphenated properties use style blocks, NOT dot notation
- [ ] Numeric values are within valid ranges
- [ ] Style blocks use proper syntax: `node.style: { property: value }`

### Structure
- [ ] All braces are matched
- [ ] All colons have values after them
- [ ] Classes are defined inside `classes: {}` block

## Debug Tips

1. **Start minimal** - Begin with basic structure, add styles incrementally
2. **Use D2 Playground** - [play.d2lang.com](https://play.d2lang.com/) for immediate feedback
3. **Test style blocks** - If dot notation fails, wrap in a style block
