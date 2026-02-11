# Icons and Images in D2

D2 supports adding external images and icons to your diagrams.

## Basic Image Syntax

```
node.icon: https://example.com/image.png
```

## Image Sources

### URLs

```
logo.icon: https://example.com/logo.png
avatar.icon: https://cdn.example.com/user.png
```

### Local Files

```
localIcon.icon: ./assets/icon.png
diagramIcon.icon: /path/to/images/diagram.svg
```

### Data URLs

```
embedded.icon: data:image/svg+xml;base64,PHN2Zy...
```

## Icon Sizing

### Width

```
# Specify width in pixels
smallIcon.icon: https://example.com/icon.png { width: 32 }
mediumIcon.icon: https://example.com/icon.png { width: 64 }
largeIcon.icon: https://example.com/icon.png { width: 128 }
```

### Height

```
# Specify height
tallIcon.icon: https://example.com/icon.png { height: 100 }
```

### Both Dimensions

```
# Both width and height
exactSize.icon: https://example.com/icon.png {
  width: 100
  height: 50
}
```

## Complete Examples

### Company Logo

```
direction: right

Company: {
  icon: https://via.placeholder.com/100x50/2196f3/ffffff?text=Company
  style: { stroke: none }
}

Company -> Product
Company -> Service
```

### User Avatars

```
direction: right

# User nodes with avatars
alice: {
  icon: https://i.pravatar.cc/150?u=a
  label: Alice
}

bob: {
  icon: https://i.pravatar.cc/150?u=b
  label: Bob
}

charlie: {
  icon: https://i.pravatar.cc/150?u=c
  label: Charlie
}

# Relationships
alice -> bob: messages
bob -> charlie: forwards
charlie -> alice: replies
```

### Technology Stack Icons

```
# Architecture with tech icons
direction: down

frontend: {
  icon: https://cdn.jsdelivr.net/gh/devicons/devicon/icons/react/react-original.svg
  label: React Frontend
  icon-width: 80
}

api: {
  icon: https://cdn.jsdelivr.net/gh/devicons/devicon/icons/nodejs/nodejs-original.svg
  label: Node.js API
  icon-width: 80
}

database: {
  icon: https://cdn.jsdelivr.net/gh/devicons/devicon/icons/postgresql/postgresql-original.svg
  label: PostgreSQL
  icon-width: 80
}

cache: {
  icon: https://cdn.jsdelivr.net/gh/devicons/devicon/icons/redis/redis-original.svg
  label: Redis Cache
  icon-width: 80
}

# Flow
frontend -> api
api -> database
api -> cache
```

### Cloud Provider Icons

```
# AWS Architecture
direction: right

# AWS Load Balancer
alb: {
  icon: https://cdn.jsdelivr.net/gh/devicons/devicon/icons/amazonwebservices/amazonwebservices-plain-wordmark.svg
  label: ALB
  icon-width: 60
}

# EC2 Instances
ec2_1: {
  icon: https://cdn.jsdelivr.net/gh/devicons/devicon/icons/amazonwebservices/amazonwebservices-plain-wordmark.svg
  label: EC2 1
  icon-width: 50
}

ec2_2: {
  icon: https://cdn.jsdelivr.net/gh/devicons/devicon/icons/amazonwebservices/amazonwebservices-plain-wordmark.svg
  label: EC2 2
  icon-width: 50
}

# RDS
rds: {
  icon: https://cdn.jsdelivr.net/gh/devicons/devicon/icons/amazonwebservices/amazonwebservices-plain-wordmark.svg
  label: RDS
  icon-width: 60
}

# Connections
alb -> ec2_1
alb -> ec2_2
ec2_1 -> rds
ec2_2 -> rds
```

## SVG Icons

### Using SVG

```
# Inline SVG (via data URL)
checkIcon: {
  icon: data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%234caf50'%3E%3Cpath d='M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z'/%3E%3C/svg%3E
  style: { stroke: none }
}
```

### Using Icon Libraries

Popular icon sources:

**Devicons** (Development/Technology)
```
https://cdn.jsdelivr.net/gh/devicons/devicon/icons/[name]/[file]
```

Examples:
- React: `icons/react/react-original.svg`
- Node.js: `icons/nodejs/nodejs-original.svg`
- Python: `icons/python/python-original.svg`
- Docker: `icons/docker/docker-original.svg`

**Simple Icons** (Brand logos)
```
https://cdn.simpleicons.org/[name]
```

Examples:
- GitHub: `https://cdn.simpleicons.org/github`
- Google: `https://cdn.simpleicons.org/google`
- AWS: `https://cdn.simpleicons.org/amazonaws`

**Font Awesome** (via CDN)
```
https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/svgs/solid/[icon].svg
```

## Image Best Practices

1. **Use SVG when possible** - Scales better than PNG/JPG
2. **Keep file sizes small** - Large images slow down rendering
3. **Use consistent sizing** - Same icon types should be same size
4. **Consider contrast** - Icons should be visible against backgrounds
5. **Add labels** - Icons alone may not be clear
6. **Test URLs** - External links may break or be blocked

## Icon Styling

```
# Remove borders on icon-only nodes
iconNode.style: { stroke: none }

# Add background
iconNode.style: {
  fill: #f5f5f5
  stroke: #ddd
}

# Add shadow
iconNode.style: { shadow: true }
```

## Common Patterns

### Status Icons

```
# Success, warning, error states
success: {
  icon: https://cdn.jsdelivr.net/npm/@mdi/svg@7.2.96/svg/check-circle.svg
  style: { fill: #c8e6c9; stroke: #4caf50 }
}

warning: {
  icon: https://cdn.jsdelivr.net/npm/@mdi/svg@7.2.96/svg/alert.svg
  style: { fill: #fff9c4; stroke: #ff9800 }
}

error: {
  icon: https://cdn.jsdelivr.net/npm/@mdi/svg@7.2.96/svg/alert-circle.svg
  style: { fill: #ffcdd2; stroke: #f44336 }
}
```

### Social Media Links

```
direction: right

github: {
  icon: https://cdn.simpleicons.org/github
  label: GitHub
}

twitter: {
  icon: https://cdn.simpleicons.org/twitter
  label: @user
}

linkedin: {
  icon: https://cdn.simpleicons.org/linkedin
  label: LinkedIn
}

github -> twitter -> linkedin
```

## Tips

1. For production diagrams, host icons on your own CDN
2. Use data URLs for small, simple icons
3. Consider creating your own icon set for consistency
4. Test diagram rendering with different image sources
5. Have fallback labels in case images don't load
