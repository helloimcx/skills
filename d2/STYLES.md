# D2 Style Properties Reference

## Critical Syntax Rule

**Hyphenated properties must use style blocks, not dot notation.**

| Property | Valid Range |
|----------|-------------|
| stroke-width | 1-15 |
| stroke-dash | 0-10 |
| border-radius | 0-20 |
| font-size | 8-100 |
| font-weight | (bold, normal) |
| font-style | (italic, normal) |
| text-align | (left, center, right) |
| vertical-align | (top, middle, bottom) |
| fill-pattern | (none, dots, lines) |

**WRONG:**
```d2
node.style.stroke-width: 2        # ERROR
node.style.font-size: 14           # ERROR
```

**CORRECT:**
```d2
node.style: {
  stroke-width: 2
  font-size: 14
}
```

**Non-hyphenated properties work with dot notation:**
```d2
node.style.fill: red               # OK
node.style.stroke: blue            # OK
node.style.font-color: white       # OK
node.style.opacity: 80             # OK
```

## Color Properties

### Fill (Background)
```d2
# Style block
node.style: {
  fill: red
  fill: #ff0000
  fill: rgb(255, 0, 0)
  fill: rgba(255, 0, 0, 0.5)
}

# Dot notation
node.style.fill: lightblue
```

### Stroke (Border)
```d2
node.style: { stroke: blue }
edge.style: { stroke: #0088cc }
```

### Font Color
```d2
node.style: { font-color: white }
```

## Stroke Properties

### Stroke Width (1-15)
```d2
edge.style: {
  stroke-width: 1   # Thin
  stroke-width: 2   # Default
  stroke-width: 4   # Thick
}
```

### Stroke Dash (0-10)
```d2
edge.style: {
  stroke-dash: 0   # Solid
  stroke-dash: 3   # Dotted
  stroke-dash: 5   # Dashed
  stroke-dash: 10  # Long dash
}
```

## Visual Effects

### Opacity (0-100)
```d2
node.style: { opacity: 50 }
```

### Shadow
```d2
node.style: { shadow: true }
```

### Border Radius (0-20)
```d2
node.style: { border-radius: 10 }
```

### Fill Pattern
```d2
node.style: { fill-pattern: dots }
node.style: { fill-pattern: lines }
```

## Font Properties

### Font Family
```d2
node.style: { font: Arial }
node.style: { font: "Helvetica Neue" }
node.style: { font: monospace }
```

### Font Size (8-100)
```d2
node.style: { font-size: 12 }
```

### Font Weight
```d2
node.style: { font-weight: bold }
node.style: { font-weight: normal }
```

### Font Style
```d2
node.style: { font-style: italic }
node.style: { font-style: normal }
```

## Text Alignment

```d2
node.style: {
  text-align: center
  vertical-align: middle
}
```

## Dimensions

```d2
node.style: {
  width: 100
  height: 50
  min-width: 200
  min-height: 100
}
```

## Common Style Patterns

### Success/Error States
```d2
success.style: { fill: #c8e6c9; stroke: #4caf50 }
error.style: { fill: #ffcdd2; stroke: #f44336 }
warning.style: { fill: #fff9c4; stroke: #ffc107 }
```

### Database Tables
```d2
sql_table.style: {
  fill: #e3f2fd
  stroke: #1976d2
  stroke-width: 2
  border-radius: 4
}
```

### Services/Components
```d2
service.style: {
  fill: #fff3e0
  stroke: #ef6c00
  stroke-width: 2
}
```

### Decision Points
```d2
decision.style: {
  fill: #fff9c4
  stroke: #f57f17
}
```

### External Systems
```d2
external.style: {
  fill: #eceff1
  stroke: #546e7a
  stroke-dash: 5
}
```

## Global Styles

```d2
# All connections
connection.style.stroke: #666
connection.style.stroke-width: 2

# All containers
container.style.fill: #f5f5f5
container.style.stroke: #999
```
