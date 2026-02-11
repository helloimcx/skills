# UML Class Diagrams in D2

D2 provides special syntax for UML class diagrams with fields, methods, and visibility markers.

## Basic Class Syntax

```
# Simple class
User: class {
  name: string
  email: string
  login(): bool
  logout(): void
}
```

## Field Syntax

```
ClassName: class {
  field_name: type
  optional_field?: type
  collection: type[]
}
```

## Method Syntax

```
ClassName: class {
  method_name(): return_type
  method_with_params(param: type): return_type
}
```

## Visibility Modifiers

Use UML standard prefixes:

| Prefix | Visibility | Symbol |
|--------|------------|--------|
| `+` | Public | `+` |
| `-` | Private | `-` |
| `#` | Protected | `#` |
| `~` | Package | `~` |

```
User: class {
  +id: int
  +name: string
  -password: string
  #validate(): bool
}
```

## Class Relationships

### Association (Basic)

```
User -> Profile
```

### Composition (Filled Diamond)

```
Order ->* Product  # Order contains Products
```

### Aggregation (Empty Diamond)

```
Department o-> Employee  # Department has Employees
```

### Inheritance (Hollow Triangle)

```
Employee ->> Person  # Employee extends Person
```

### Implementation (Dashed Line)

```
Serializable ..|> SomeInterface
```

### Dependency (Dashed)

```
Controller ..> Service
```

## Relationship Labels

```
User -> Role: has a
User -> Account: uses
```

## Cardinality

```
User "1" -> "0..*" Order  # One user has many orders
Order "1" -> "1" User     # Each order has one user
```

Cardinality patterns:
- `1` - Exactly one
- `0..1` - Zero or one
- `*` or `0..*` - Zero or more
- `1..*` - One or more
- `n` - Exactly n

## Complete Example

```
# Abstract class
Person: class {
  +id: int
  +name: string
  #getDetails(): string
}

# Subclass
Employee: class {
  +employeeId: string
  +department: string
  +getSalary(): float
}

Employee ->> Person: extends

# Another subclass
Customer: class {
  +customerId: string
  +email: string
  +getOrders(): Order[]
}

Customer ->> Person: extends

# Related class
Order: class {
  +orderId: int
  +orderDate: date
  +total: decimal
  +addItem(item: Item): void
  +checkout(): bool
}

Customer "1" -> "0..*" Order: places
Order ->* OrderItem: contains

Product: class {
  +sku: string
  +name: string
  +price: decimal
  +stock: int
}

OrderItem "1..*" -> "1" Product: refers to
```

## Abstract Classes

D2 doesn't have explicit abstract syntax, but use naming convention:

```
AbstractEntity: class {
  +id: int
  +createdAt: timestamp
  #validate(): bool
}

ConcreteEntity: class {
  +specificField: string
}

ConcreteEntity ->> AbstractEntity
```

## Interfaces

```
interface: class {
  +method1(): void
  +method2(): bool
}

Implementation: class {
  +method1(): void
  +method2(): bool
  +extraMethod(): void
}

Implementation ..|> interface: implements
```

## Nested Classes

```
Outer: class {
  +outerField: string
}

Outer.Inner: class {
  +innerField: string
}
```

## Tips

1. Use `+` for public API members
2. Use `-` for private implementation details
3. Use `#` for protected methods meant for subclasses
4. Add return types to methods for clarity
5. Label relationships to explain domain concepts
6. Use cardinality to show constraints
