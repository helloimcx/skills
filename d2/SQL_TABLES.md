# Database Diagrams (ERD) in D2

D2 provides built-in support for database entity-relationship diagrams using the `sql_table` shape.

## Basic Table Syntax

```
users: sql_table {
  id: int
  name: string
  email: string
}
```

## Column Constraints

Add constraints after the type:

```
users: sql_table {
  id: int PK          # Primary Key
  email: string UNQ   # Unique
  name: string NN     # Not Null
  role_id: int FK     # Foreign Key
}
```

Constraint codes:
- `PK` - Primary Key
- `FK` - Foreign Key
- `UNQ` - Unique constraint
- `NN` - Not Null

## Complete Table Example

```
users: sql_table {
  id: int PK
  username: string UNQ NN
  email: string UNQ NN
  password_hash: string NN
  created_at: timestamp
  updated_at: timestamp
}

roles: sql_table {
  id: int PK
  name: string UNQ NN
  permissions: string
}

user_roles: sql_table {
  user_id: int FK
  role_id: int FK
  assigned_at: timestamp
}
```

## Table Relationships

### One-to-Many

```
# Department has many Employees
Department -> Employee
```

### Many-to-Many

```
# Users <-> Roles via junction table
User -> user_roles
Role -> user_roles
```

With labels:
```
User -> user_roles: foreign key
Role -> user_roles: foreign key
```

## Complete E-Commerce Schema

```
customers: sql_table {
  id: int PK
  email: string UNQ NN
  name: string NN
  phone: string
  created_at: timestamp
}

orders: sql_table {
  id: int PK
  customer_id: int FK NN
  order_date: timestamp NN
  status: string NN
  total: decimal
}

products: sql_table {
  id: int PK
  sku: string UNQ NN
  name: string NN
  description: text
  price: decimal NN
  stock: int
  category_id: int FK
}

order_items: sql_table {
  id: int PK
  order_id: int FK NN
  product_id: int FK NN
  quantity: int NN
  unit_price: decimal
}

categories: sql_table {
  id: int PK
  name: string UNQ NN
  parent_id: int FK
}

payments: sql_table {
  id: int PK
  order_id: int FK UNQ
  amount: decimal NN
  payment_method: string NN
  status: string
  processed_at: timestamp
}

shipping_addresses: sql_table {
  id: int PK
  customer_id: int FK
  address_line1: string NN
  address_line2: string
  city: string NN
  state: string
  postal_code: string
  country: string NN
}

# Relationships
customers -> orders: places
orders -> order_items: contains
products -> order_items: ordered in
categories -> products: categorizes
orders -> payments: paid by
customers -> shipping_addresses: has
categories -> categories: parent of
```

## Styling Tables

```
# Style specific table
users.style.fill: #e3f2fd

# Style all tables
sql_table.style: {
  fill: #f5f5f5
  stroke: #333
  stroke-width: 2
}
```

## Advanced: Multiple Column Types

```
audit_log: sql_table {
  id: int PK
  table_name: string NN
  record_id: int NN
  action: string NN  # INSERT, UPDATE, DELETE
  old_values: json
  new_values: json
  changed_by: int FK
  changed_at: timestamp NN
}
```

## Tips

1. Always mark primary keys with `PK`
2. Mark foreign keys with `FK` for clarity
3. Use `UNQ` for unique constraints (email, username)
4. Use `NN` for required fields
5. Junction tables for many-to-many relationships should be named clearly
6. Consider adding timestamp columns for audit trails
7. Group related tables in containers for large schemas
