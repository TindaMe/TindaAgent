---
name: fastapi-best-practices
category: Backend
description: "MUST USE when creating or editing FastAPI routes, dependencies, Pydantic schemas, middleware, or API configuration. Enforces async correctness, dependency injection, service layer, Pydantic validation, and structured error handling."
---

# FastAPI Best Practices

## Project Structure

Organize by domain. Each domain owns its routes, schemas, models, and services.

```
src/
├── auth/
│   ├── router.py
│   ├── schemas.py
│   ├── models.py
│   ├── service.py
│   ├── dependencies.py
│   └── exceptions.py
├── posts/
│   └── (same structure)
├── config.py
├── database.py
└── main.py
```

```python
# GOOD: namespace imports across domains
from src.auth import service as auth_service
from src.posts import service as post_service
```

## Async Correctness

```python
# BAD: blocks the event loop
@router.get("/sleep")
async def bad_sleep():
    time.sleep(10)  # all other requests stall

# GOOD: sync def — FastAPI offloads to threadpool
@router.get("/sleep")
def good_sleep_sync():
    time.sleep(10)

# GOOD: async with non-blocking I/O
@router.get("/sleep")
async def good_sleep_async():
    await asyncio.sleep(10)

# GOOD: wrap sync SDKs
@router.get("/external")
async def good_external():
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, partial(sync_sdk.call))
    return result
```

- `async def`: only with `await`-able I/O (async DB, httpx, aiofiles)
- `def`: blocking I/O (sync DB, `requests`, file I/O, CPU-bound)

## Pydantic Schemas

```python
# BAD: one model for everything — leaks hashed_password
class User(BaseModel):
    id: int
    email: str
    hashed_password: str

# GOOD: separate schemas per operation
class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

class UserRead(BaseModel):
    id: int
    email: EmailStr
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)
```

```python
# GOOD: always use response_model
@router.get("/users/{user_id}", response_model=UserRead)
async def get_user(user_id: int):
    return await user_service.get_by_id(user_id)
```

## Dependency Injection

```python
# Reusable type aliases
CurrentUser = Annotated[User, Depends(get_current_active_user)]
DBSession = Annotated[AsyncSession, Depends(get_db)]

@router.get("/me", response_model=UserRead)
async def read_me(current_user: CurrentUser):
    return current_user
```

### Chain Dependencies for Validation

```python
async def valid_post_id(post_id: int, db: DBSession) -> Post:
    post = await post_service.get_by_id(db, post_id)
    if not post:
        raise PostNotFound()
    return post

async def valid_owned_post(
    post: Annotated[Post, Depends(valid_post_id)],
    current_user: CurrentUser,
) -> Post:
    if post.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not the post owner")
    return post

@router.put("/posts/{post_id}", response_model=PostRead)
async def update_post(
    post: Annotated[Post, Depends(valid_owned_post)],
    data: PostUpdate,
    db: DBSession,
):
    return await post_service.update(db, post, data)
```

## Service Layer — Keep Routes Thin

```python
# BAD: business logic in route
@router.post("/orders", response_model=OrderRead)
async def create_order(data: OrderCreate, db: DBSession, user: CurrentUser):
    for item in data.items:
        product = await db.get(Product, item.product_id)
        if product.stock < item.quantity:
            raise HTTPException(status_code=400, detail="Out of stock")
        product.stock -= item.quantity
    order = Order(user_id=user.id, **data.model_dump())
    db.add(order)
    await db.commit()
    return order

# GOOD: route delegates to service
@router.post("/orders", response_model=OrderRead, status_code=201)
async def create_order(data: OrderCreate, db: DBSession, user: CurrentUser):
    return await order_service.create_order(db, user=user, data=data)
```

## Error Handling

```python
# Custom exceptions per domain
class PostNotFound(HTTPException):
    def __init__(self):
        super().__init__(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")

# Never leak internals
@app.exception_handler(Exception)
async def generic_handler(request, exc):
    logger.exception("Unhandled error", exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
```

## Configuration

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 30
    debug: bool = False
    model_config = SettingsConfigDict(env_file=".env")

@lru_cache
def get_settings():
    return Settings()

AppSettings = Annotated[Settings, Depends(get_settings)]
```

## CORS

```python
# BAD: wildcard with credentials
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True)

# GOOD: explicit origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://app.example.com"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)
```

## Authentication

```python
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)], db: DBSession,
) -> User:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        user_id: int = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid token")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Could not validate credentials",
                            headers={"WWW-Authenticate": "Bearer"})
    user = await user_service.get_by_id(db, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user
```

## Rules

1. **Organize by domain** — each feature owns its routes, schemas, models, services, exceptions
2. **Never block the event loop** — use `def` for sync I/O, `async def` only with `await`-able calls
3. **Separate schemas** for create, read, and internal use — never expose internal fields
4. **Use `response_model`** on every route
5. **Use `Annotated[T, Depends()]`** — create reusable type aliases
6. **Chain dependencies** for validation and authorization — keep routes thin
7. **Service layer** for business logic — routes only handle HTTP concerns
8. **Custom exceptions per domain** — never leak internal error details
9. **`BaseSettings` + `lru_cache`** for config — never hardcode secrets
10. **Explicit CORS origins** — never `"*"` with `allow_credentials=True`
