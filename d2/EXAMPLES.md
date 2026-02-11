# D2 Diagram Examples

Common diagram patterns and examples you can adapt.

## Flowchart

```
# User Login Flowchart
direction: down

Start: {
  shape: oval
  style: { fill: #c8e6c9 }
}

End: {
  shape: oval
  style: { fill: #ffcdd2 }
}

EnterCredentials: {
  shape: rectangle
  style: { fill: #e3f2fd }
}

Validate: {
  shape: diamond
  style: { fill: #fff9c4 }
}

ShowError: {
  shape: rectangle
  style: { fill: #ffe0b2 }
}

Dashboard: {
  shape: rectangle
  style: { fill: #e3f2fd }
}

ResetPassword: {
  shape: rectangle
  style: { fill: #f3e5f5 }
}

# Flow
Start -> EnterCredentials: User visits
EnterCredentials -> Validate: Submit form
Validate -> ShowError: Invalid
Validate -> Dashboard: Valid
ShowError -> EnterCredentials: Retry
EnterCredentials -> ResetPassword: Forgot password
ResetPassword -> Start
Dashboard -> End: Logout
```

## System Architecture

```
# Web Application Architecture
direction: right

# Client Layer
clients: {
  style: { fill: #e8f5e9 }

  "Web Browser": {
    icon: https://via.placeholder.com/48
  }

  "Mobile App": {
    icon: https://via.placeholder.com/48
  }
}

# Load Balancer
LoadBalancer: {
  shape: hexagon
  style: { fill: #fff3e0; stroke: #ef6c00 }
}

# Web Servers
web_servers: {
  style: { fill: #e3f2fd }
  shape: cylinder

  "Web Server 1"
  "Web Server 2"
  "Web Server 3"
}

# Application Layer
app_servers: {
  style: { fill: #f3e5f5 }

  "API Gateway"
  "Auth Service"
  "Business Logic"
}

# Data Layer
databases: {
  style: { fill: #ffebee }

  "Primary DB": {
    shape: database
  }

  "Cache": {
    shape: cylinder
    style: { fill: #fff9c4 }
  }

  "Message Queue": {
    shape: queue
  }
}

# External Services
external: {
  style: { fill: #eceff1; stroke-dash: 5 }

  "Payment Gateway"
  "Email Service"
}

# Connections
clients -> LoadBalancer
LoadBalancer -> web_servers
web_servers -> app_servers
app_servers -> databases
app_servers -> external
```

## Microservices Architecture

```
direction: down

# External gateways
"API Gateway": {
  shape: hexagon
  style: { fill: #e1f5fe }
}

# Services container
services: {
  style: { fill: #f5f5f5 }

  "Auth Service": {
    shape: rectangle
    style: { fill: #c8e6c9 }
  }

  "User Service": {
    shape: rectangle
    style: { fill: #c8e6c9 }
  }

  "Order Service": {
    shape: rectangle
    style: { fill: #c8e6c9 }
  }

  "Payment Service": {
    shape: rectangle
    style: { fill: #c8e6c9 }
  }

  "Notification Service": {
    shape: rectangle
    style: { fill: #c8e6c9 }
  }
}

# Data layer
data: {
  style: { fill: #f5f5f5 }

  "User DB": {
    shape: database
    style: { fill: #ffccbc }
  }

  "Order DB": {
    shape: database
    style: { fill: #ffccbc }
  }

  "Cache": {
    shape: cylinder
    style: { fill: #fff9c4 }
  }

  "Message Broker": {
    shape: queue
    style: { fill: #b2dfdb }
  }
}

# External
"External APIs": {
  style: { fill: #e0e0e0; stroke-dash: 5 }
}

# Flow
"API Gateway" -> services
services -> data
services -> "External APIs"
"Notification Service" -> "Message Broker"
```

## Nested Containers

```
# Company Organization
direction: down

Company: {
  style: { fill: #f5f5f5 }

  Executive: {
    style: { fill: #e8f5e9 }

    CEO: {
      shape: person
    }

    CTO: {
      shape: person
    }

    CFO: {
      shape: person
    }
  }

  Engineering: {
    style: { fill: #e3f2fd }

    "Frontend Team"
    "Backend Team"
    "DevOps Team"
  }

  Product: {
    style: { fill: #f3e5f5 }

    "Design Team"
    "Product Managers"
  }

  Operations: {
    style: { fill: #fff3e0 }

    "HR"
    "Sales"
    "Marketing"
  }
}

CEO -> CTO
CEO -> CFO
CTO -> Engineering
CEO -> Product
CFO -> Operations
```

## Class Diagram

```
# Payment System Class Diagram
direction: down

# Abstract base
PaymentProcessor: class {
  +process(amount: decimal): bool
  +refund(transaction_id: string): bool
  #validate(card: Card): bool
}

# Concrete implementations
CreditCardProcessor: class {
  +process(amount: decimal): bool
  +refund(transaction_id: string): bool
  -validateCard(card: Card): bool
  -authorize(): string
}

PayPalProcessor: class {
  +process(amount: decimal): bool
  +refund(transaction_id: string): bool
  -getAccessToken(): string
}

# Related classes
Transaction: class {
  +id: string
  +amount: decimal
  +status: TransactionStatus
  +createdAt: DateTime
  +capture(): void
  +void(): void
}

Card: class {
  +number: string
  +expiry: string
  +cvv: string
  +isValid(): bool
}

PaymentResult: class {
  +success: bool
  +transactionId: string
  +errorCode: string
  +errorMessage: string
}

# Relationships
CreditCardProcessor ->> PaymentProcessor: extends
PayPalProcessor ->> PaymentProcessor: extends
PaymentProcessor ..> Transaction: creates
CreditCardProcessor ..> Card: uses
PaymentProcessor -> PaymentResult: returns
Transaction ->* PaymentResult: produces
```

## Deployment Diagram

```
# Production Deployment
direction: right

# Regions
us_region: {
  label: US-East
  style: { fill: #e3f2fd }

  us_lb: {
    shape: hexagon
    label: Load Balancer
  }

  us_apps: {
    label: App Servers (3x)
    shape: cylinder
  }

  us_db: {
    shape: database
    label: Primary DB
    style: { fill: #ffccbc }
  }
}

eu_region: {
  label: EU-West
  style: { fill: #c8e6c9 }

  eu_lb: {
    shape: hexagon
    label: Load Balancer
  }

  eu_apps: {
    label: App Servers (2x)
    shape: cylinder
  }

  eu_db: {
    shape: database
    label: Standby DB
    style: { fill: #ffccbc }
  }
}

# CDN
cdn: {
  label: CDN
  style: { fill: #fff9c4 }
  shape: cylinder
}

# Monitoring
monitoring: {
  label: Monitoring
  style: { fill: #f3e5f5 }
}

# Connections
cdn -> us_lb
cdn -> eu_lb
us_lb -> us_apps
eu_lb -> eu_apps
us_apps -> us_db
eu_apps -> eu_db
us_db -> eu_db: replication
us_region -> monitoring: metrics
eu_region -> monitoring: metrics
```

## State Diagram

```
# Order State Machine
direction: right

# States
"Pending": {
  style: { fill: #e3f2fd }
}

"Confirmed": {
  style: { fill: #fff9c4 }
}

"Processing": {
  style: { fill: #ffe0b2 }
}

"Shipped": {
  style: { fill: #c8e6c9 }
}

"Delivered": {
  style: { fill: #a5d6a7 }
}

"Cancelled": {
  style: { fill: #ffcdd2 }
}

"Refunded": {
  style: { fill: #f48fb1 }
}

# Transitions
"Pending" -> "Confirmed": payment received
"Pending" -> "Cancelled": timeout/decline
"Confirmed" -> "Processing": preparing
"Processing" -> "Shipped": dispatched
"Shipped" -> "Delivered": received
"Confirmed" -> "Cancelled": customer request
"Processing" -> "Cancelled": customer request
"Delivered" -> "Refunded": return requested
"Cancelled" -> "Refunded": payment reversal
```

## Tips

1. Use `direction: down` or `direction: right` to control flow
2. Group related elements in containers
3. Use consistent colors for similar element types
4. Labels make diagrams self-documenting
5. Keep diagrams focused - split complex ones into multiple
