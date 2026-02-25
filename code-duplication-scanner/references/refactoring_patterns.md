# Refactoring Patterns Reference

Quick reference for common refactoring patterns when code duplication is detected.

## Extract Method

**When to use**: New code duplicates logic from an existing method

**Example**:
```java
// Existing code in OldService.java
public void processData(Data data) {
    validate(data);
    transform(data);
    save(data);
}

// Your new code in NewService.java (DUPLICATE!)
public void handleData(Data data) {
    validate(data);      // same
    transform(data);     // same
    save(data);          // same
}

// Refactor: Call existing method or extract to common class
public void handleData(Data data) {
    oldService.processData(data);  // Option 1: delegate
}
// OR extract to BaseService and have both extend it
```

## Extract Utility Class

**When to use**: Same logic used across multiple unrelated classes

**Example**:
```java
// Extract to DataUtils.java
public class DataUtils {
    public static void validateAndSave(Data data) {
        validate(data);
        save(data);
    }
}

// Then use in both places:
DataUtils.validateAndSave(data);
```

## Use Existing Method

**When to use**: Your new code duplicates an existing method

**Example**:
```java
// Instead of duplicating:
public double calculateTotal(List<Item> items) {
    return items.stream().mapToDouble(Item::getPrice).sum();
}

// Use existing:
OrderCalculator.calculateTotal(items);
```

## Consolidate Conditional

**When to use**: Same condition check appears in multiple places

**Example**:
```java
// Before
if (user.isActive() && user.hasPermission("read")) { ... }

// After
if (user.canRead()) { ... }  // Extract to method
```

## Strategy Pattern

**When to use**: Similar algorithms with slight variations

**Example**:
```java
interface DataProcessor {
    void process(Data data);
}

class XmlProcessor implements DataProcessor { ... }
class JsonProcessor implements DataProcessor { ... }
```

## Language-Specific Tips

### Java
- Use Lombok to reduce getter/setter boilerplate
- Consider default methods in interfaces for shared logic
- Use utility classes with static methods

### JavaScript/TypeScript
- Export shared functions from utility modules
- Use higher-order functions to reduce duplication
- Consider composition over inheritance

### Python
- Use decorators for cross-cutting concerns
- Extract common logic to module-level functions
- Use mixins for shared behavior
