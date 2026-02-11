# D2 Classes - CSS-like Reusability

D2 supports a class system similar to CSS, allowing you to define reusable styles and apply them to multiple objects.

## Defining Classes

Classes must be defined inside a `classes: {}` block:

```
classes: {
  warning: {
    style: {
      fill: #fff3cd
      stroke: #ffc107
      stroke-width: 2
    }
  }
}
```

## Applying Classes

### Single Class

```
errorNode.class: warning
```

### Multiple Classes (Use Semicolons!)

```
# Apply multiple classes (semicolon-separated)
importantNode.class: [warning; bold]
```

**Critical**: Multiple classes must use **semicolons** (`;`), not commas (`,`).

## Complete Class Example

```
# Define classes in a classes block
classes: {
  primary: {
    style: {
      fill: #e3f2fd
      stroke: #2196f3
      stroke-width: 2
    }
  }

  success: {
    style: {
      fill: #c8e6c9
      stroke: #4caf50
      stroke-width: 2
    }
  }

  danger: {
    style: {
      fill: #ffcdd2
      stroke: #f44336
      stroke-width: 2
    }
  }

  warning: {
    style: {
      fill: #fff9c4
      stroke: #ff9800
      stroke-width: 2
    }
  }

  bold: {
    style: {
      font-weight: bold
    }
  }

  large: {
    style: {
      font-size: 16
    }
  }
}

# Apply classes
loginPage.class: primary
submitBtn.class: [primary; bold; large]
errorMsg.class: [danger; bold]
successMsg.class: [success; large]
warningMsg.class: [warning; bold]
```

## Connection Classes

You can also apply classes to connections:

```
# Define connection class
dashed.style: {
  stroke-dash: 5
  stroke: #999
}

# Apply to connection
A -> B.class: dashed
```

## Class Inheritance and Overriding

When multiple classes are applied, later classes can override earlier ones:

```
base.style: {
  fill: white
  stroke: black
  stroke-width: 1
}

important.style: {
  stroke: red
  stroke-width: 3
}

# Apply both - important overrides stroke properties (use semicolons!)
criticalNode.class: [base; important]
# Result: fill=white, stroke=red, stroke-width=3
```

## Class Organization

### By Purpose

```
# Semantic classes
button.style: { ... }
input.style: { ... }
label.style: { ... }

# State classes
active.style: { ... }
disabled.style: { ... }
error.style: { ... }

# Size classes
small.style: { ... }
medium.style: { ... }
large.style: { ... }
```

### By Component Type

```
# API components
endpoint.style: {
  fill: #f3e5f5
  stroke: #7b1fa2
}

database.style: {
  fill: #e3f2fd
  stroke: #1976d2
}

cache.style: {
  fill: #fff9c4
  stroke: #f9a825
}
```

## Real-World Example

```
# Define style system
# =============

# Colors
color-blue.style: { fill: #e3f2fd; stroke: #1976d2 }
color-green.style: { fill: #c8e6c9; stroke: #4caf50 }
color-red.style: { fill: #ffcdd2; stroke: #f44336 }
color-yellow.style: { fill: #fff9c4; stroke: #ff9800 }
color-gray.style: { fill: #f5f5f5; stroke: #9e9e9e }

# Shapes
shape-round.style: { border-radius: 10 }
shape-sharp.style: { border-radius: 0 }
shadow-on.style: { shadow: true }

# Typography
text-bold.style: { font-weight: bold }
text-large.style: { font-size: 16 }
text-small.style: { font-size: 10 }

# Lines
line-dashed.style: { stroke-dash: 5 }
line-dotted.style: { stroke-dash: 3 }
line-thick.style: { stroke-width: 3 }

# Apply to diagram
# ================

# User flow nodes (use semicolons!)
startNode.class: [color-green; shape-round; text-bold]
endNode.class: [color-red; shape-round]
processNode.class: [color-blue; shape-round; shadow-on]
decisionNode.class: [color-yellow; shape-sharp]

# Connections
dataFlow.class: line-thick
controlFlow.class: line-dashed

# External systems
externalSystem.class: [color-gray, line-dashed]
```

## Advanced: Class-like Patterns

### Using Substitutions for Reusable Components

```
# Define a reusable "component"
microservice: &service {
  shape: rectangle
  style: {
    fill: #e3f2fd
    stroke: #2196f3
    stroke-width: 2
    border-radius: 8
  }
}

# Use it (Note: D2 doesn't have true YAML anchors,
# but you can structure your definitions this way)
```

### Style Functions Pattern

```
# Group styles by semantic meaning
authenticated: {
  style: { fill: #c8e6c9; stroke: #4caf50 }
}

guest: {
  style: { fill: #fff9c4; stroke: #ff9800 }
}

admin: {
  style: { fill: #f3e5f5; stroke: #7b1fa2 }
}

# Apply based on user type
userHome.class: authenticated
landingPage.class: guest
adminPanel.class: admin
```

## Best Practices

1. **Name classes semantically** - `primary`, `success`, `danger` instead of `blue-box`, `green-box`
2. **Keep classes focused** - One class should do one thing well
3. **Use consistent naming** - Pick a convention and stick to it
4. **Document your classes** - Comment groups of related classes
5. **Compose over customize** - Combine multiple simple classes rather than creating complex ones
6. **Order matters** - When using multiple classes, put general ones first, specific ones last

## Example Color Palette

```
# Material Design inspired palette
# =================================

# Red
red-50.style: { fill: #ffebee }
red-100.style: { fill: #ffcdd2 }
red-500.style: { fill: #f44336; stroke: #d32f2f }
red-700.style: { fill: #e53935 }

# Blue
blue-50.style: { fill: #e3f2fd }
blue-100.style: { fill: #bbdefb }
blue-500.style: { fill: #2196f3; stroke: #1976d2 }
blue-700.style: { fill: #1976d2 }

# Green
green-50.style: { fill: #e8f5e9 }
green-100.style: { fill: #c8e6c9 }
green-500.style: { fill: #4caf50; stroke: #388e3c }
green-700.style: { fill: #388e3c }

# Amber (Warning)
amber-50.style: { fill: #fff8e1 }
amber-100.style: { fill: #ffecb3 }
amber-500.style: { fill: #ffc107; stroke: #ff8f00 }
amber-700.style: { fill: #ff8f00 }

# Gray
gray-50.style: { fill: #fafafa }
gray-100.style: { fill: #f5f5f5 }
gray-500.style: { fill: #9e9e9e; stroke: #616161 }
```
