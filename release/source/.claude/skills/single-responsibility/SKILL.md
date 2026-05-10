---
name: single-responsibility
category: Architecture
description: "MUST USE when creating or editing any code file in any language. Enforces Single Responsibility Principle: max 200 lines/file, max 30 lines/function, one reason to change per unit."
---

# Single Responsibility Principle (Language-Agnostic)

Every file, function, class, and module does **one thing**. If you can describe it with "and", split it.

A unit should have **one reason to change** — it serves one actor or stakeholder. If changes from the business team AND the ops team both require editing the same file, that file has two responsibilities.

## Hard Limits

| Metric | Max | Action |
|--------|-----|--------|
| File length | 200 lines | Split into smaller modules |
| Function/method body | 30 lines | Extract helpers |
| Function parameters | 3 | Use an options/config object |
| Nesting depth | 3 levels | Extract early returns or helpers |
| Cyclomatic complexity | 5 branches | Simplify or split logic |
| Class dependencies | 5 imports | Class knows too much — split it |

## Separation of Concerns

### Computation vs Side Effects

```python
# BAD: business logic mixed with I/O
def process_order(order):
    tax = order.total * 0.08
    order.total += tax
    db.save(order)
    send_email(order.customer, f"Order {order.id} confirmed")
    log.info(f"Processed order {order.id}")

# GOOD: pure computation separated from side effects
def calculate_total_with_tax(order):
    return order.total + (order.total * 0.08)

def handle_order(order):
    order.total = calculate_total_with_tax(order)
    db.save(order)
    notify_customer(order)
```

### Query vs Command (CQS)

```typescript
// BAD: queries AND mutates in the same function
function getUserAndUpdateLogin(userId: string) {
  const user = db.find(userId);
  user.lastLogin = new Date();
  db.save(user);
  return user;
}

// GOOD: separate query from command
function getUser(userId: string) {
  return db.find(userId);
}

function recordLogin(user: User) {
  user.lastLogin = new Date();
  db.save(user);
}
```

### Policy vs Mechanism

```go
// BAD: business rules mixed with HTTP details
func PlaceOrder(cart Cart) error {
    jsonBytes, _ := json.Marshal(cart)
    req, _ := http.NewRequest("POST", url, bytes.NewReader(jsonBytes))
    req.Header.Set("Content-Type", "application/json")
    resp, _ := http.DefaultClient.Do(req)
    if resp.StatusCode == 200 {
        cart.Status = "placed"
    }
    return nil
}

// GOOD: business logic doesn't know about HTTP
func PlaceOrder(cart Cart, client OrderClient) error {
    if err := client.Submit(cart); err != nil {
        return err
    }
    cart.Status = "placed"
    return nil
}
```

## Class / Module Design

### One Resource Per Class

```typescript
// BAD: god class that formats, persists, AND validates
class Report {
  validate(data) { /* ... */ }
  calculate(data) { /* ... */ }
  formatAsHTML(data) { /* ... */ }
  formatAsPDF(data) { /* ... */ }
  saveToFile(path) { /* ... */ }
  sendByEmail(to) { /* ... */ }
}

// GOOD: each class has one reason to change
class ReportCalculator {
  calculate(data) { /* ... */ }
}

class ReportFormatter {
  toHTML(report) { /* ... */ }
  toPDF(report) { /* ... */ }
}

class ReportExporter {
  save(report, path) { /* ... */ }
  email(report, to) { /* ... */ }
}
```

### Levels of Abstraction

```python
# BAD: high-level orchestration mixed with low-level details
def deploy_service(config):
    subprocess.run(["docker", "build", "-t", config.image, "."])
    subprocess.run(["docker", "push", config.image])
    with open("k8s/deployment.yaml") as f:
        manifest = yaml.safe_load(f)
    manifest["spec"]["template"]["spec"]["containers"][0]["image"] = config.image
    subprocess.run(["kubectl", "apply", "-f", "-"],
                   input=yaml.dump(manifest).encode())
    log.info(f"Deployed {config.image}")

# GOOD: each function works at one level of abstraction
def deploy_service(config):
    build_image(config)
    push_image(config)
    update_manifest(config)

def build_image(config):
    subprocess.run(["docker", "build", "-t", config.image, "."])

def push_image(config):
    subprocess.run(["docker", "push", config.image])

def update_manifest(config):
    manifest = load_manifest("k8s/deployment.yaml")
    set_image(manifest, config.image)
    apply_manifest(manifest)
```

## Function Design

### Early Returns Over Nesting

```rust
// BAD: deep nesting
fn get_status(mission: &Mission) -> &str {
    if mission.is_active {
        if mission.has_analysis {
            if mission.analysis.is_complete {
                "complete"
            } else {
                "analyzing"
            }
        } else {
            "pending"
        }
    } else {
        "inactive"
    }
}

// GOOD: flat and scannable
fn get_status(mission: &Mission) -> &str {
    if !mission.is_active { return "inactive"; }
    if !mission.has_analysis { return "pending"; }
    if !mission.analysis.is_complete { return "analyzing"; }
    "complete"
}
```

### Pipeline Over Monolith

```python
# BAD: one function does everything
def process_data(raw_data):
    validated = []
    for item in raw_data:
        if item.get("name") and item.get("value") > 0:
            validated.append(item)
    transformed = []
    for item in validated:
        transformed.append({
            "name": item["name"].upper(),
            "value": item["value"] * 100,
        })
    grouped = {}
    for item in transformed:
        grouped.setdefault(item["name"], []).append(item)
    return grouped

# GOOD: pipeline of single-purpose functions
def validate(items):
    return [i for i in items if i.get("name") and i.get("value", 0) > 0]

def transform(items):
    return [{"name": i["name"].upper(), "value": i["value"] * 100} for i in items]

def group_by_name(items):
    groups = {}
    for item in items:
        groups.setdefault(item["name"], []).append(item)
    return groups

def process_data(raw_data):
    return group_by_name(transform(validate(raw_data)))
```

## File Organization

### One Export Focus Per File

```
# BAD: everything in one file
services/
  api.py          # handles users, orders, payments, emails

# GOOD: one concern per file
services/
  user_service.py
  order_service.py
  payment_service.py
  email_service.py
```

### Colocation Rules

- Types/interfaces shared across a module → `types` file
- Types used in one file → define in that file
- Utility used once → inline it
- Utility used 2+ times within a module → local `utils/` directory
- Utility used across modules → shared `lib/` or `common/`

## Smell Tests

Your code violates SRP if:

1. You scroll to understand a single function
2. You need comments to explain control flow
3. A test requires 4+ mocks to set up
4. Changing one thing breaks unrelated behavior
5. You describe the file with "and" (fetches data **and** formats it **and** renders it)
6. A function has more than 3 conditional branches
7. A file imports from more than 5 unrelated modules

## When NOT to Split

Don't over-apply SRP. Keep together when:

- Two things **always change together** and serve the same actor
- Splitting would create indirection with no testability gain
- The unit is under 30 lines and reads clearly as-is
- Three similar lines are better than a premature abstraction

## Rules

- Always describe a unit in one sentence without "and" — if you can't, split it.
- Always separate computation (pure logic) from side effects (I/O, logging, notifications).
- Always separate query functions from command functions (CQS).
- Never exceed 200 lines per file — split before you hit the limit.
- Never exceed 30 lines per function body — extract helpers.
- Never nest deeper than 3 levels — use early returns.
- Never mix levels of abstraction in the same function.
- Never create a class/module that serves two different stakeholders.
- Always extract when a test needs 4+ mocks — the unit has too many dependencies.
- Never split just because you can — only split when there's a real second responsibility.
