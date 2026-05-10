---
name: react-query
category: Frontend
description: "MUST USE when writing or editing: useQuery, useMutation, useInfiniteQuery, query hooks, mutation hooks, query keys, cache invalidation, or any data-fetching hook. Enforces TanStack React Query with @lukemorales/query-key-factory patterns."
---

# React Query with Query Key Factory

Use **TanStack Query** for all server state. Use **@lukemorales/query-key-factory** to manage query keys. Never use raw string arrays as query keys.

## Query Key Structure

### One File Per Feature

Each feature defines its own query keys in a dedicated file using `createQueryKeys`. Include `queryFn` directly in the key definition.

```ts
// features/todos/queries/todo-keys.ts
import { createQueryKeys } from "@lukemorales/query-key-factory"
import { todoService } from "../services/todo-service"

export const todos = createQueryKeys("todos", {
  all: {
    queryKey: null,
    queryFn: () => todoService.getAll(),
  },
  detail: (todoId: string) => ({
    queryKey: [todoId],
    queryFn: () => todoService.getById(todoId),
  }),
  list: (filters: TodoFilters) => ({
    queryKey: [{ filters }],
    queryFn: () => todoService.getList(filters),
  }),
})
```

```ts
// features/users/queries/user-keys.ts
import { createQueryKeys } from "@lukemorales/query-key-factory"
import { userService } from "../services/user-service"

export const users = createQueryKeys("users", {
  me: {
    queryKey: null,
    queryFn: () => userService.getMe(),
  },
  detail: (userId: string) => ({
    queryKey: [userId],
    queryFn: () => userService.getById(userId),
  }),
})
```

### Merge All Keys in a Single Index

Combine all feature keys with `mergeQueryKeys` in one file. This is the single source of truth for all query keys in the app.

```ts
// queries/index.ts
import { mergeQueryKeys } from "@lukemorales/query-key-factory"
import { todos } from "@/features/todos/queries/todo-keys"
import { users } from "@/features/users/queries/user-keys"

export const queries = mergeQueryKeys(todos, users)
```

**Never** create a single monolithic keys file. Each feature owns its keys.

## Custom Hooks

### One Hook Per Query or Mutation

Every query and mutation gets its own hook in its own file. Never combine multiple queries or mutations into one hook.

```ts
// features/todos/hooks/use-get-todos.ts
import { useQuery } from "@tanstack/react-query"
import { queries } from "@/queries"

export const useGetTodos = (filters: TodoFilters) => {
  return useQuery(queries.todos.list(filters))
}
```

```ts
// features/todos/hooks/use-get-todo.ts
import { useQuery } from "@tanstack/react-query"
import { queries } from "@/queries"

export const useGetTodo = (todoId: string) => {
  return useQuery({
    ...queries.todos.detail(todoId),
    enabled: !!todoId,
  })
}
```

### Mutation Hooks

Mutation hooks handle cache invalidation and updates. Use `queries.<feature>.<key>._def` to invalidate all variants of a key.

```ts
// features/todos/hooks/use-create-todo.ts
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { queries } from "@/queries"
import { todoService } from "../services/todo-service"

export const useCreateTodo = () => {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: todoService.create,
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: queries.todos._def,
      })
    },
  })
}
```

```ts
// features/todos/hooks/use-update-todo.ts
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { queries } from "@/queries"
import { todoService } from "../services/todo-service"

export const useUpdateTodo = () => {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: todoService.update,
    onSuccess: (updatedTodo) => {
      queryClient.setQueryData(
        queries.todos.detail(updatedTodo.id).queryKey,
        updatedTodo
      )
      queryClient.invalidateQueries({
        queryKey: queries.todos.list._def,
      })
    },
  })
}
```

```ts
// features/todos/hooks/use-delete-todo.ts
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { queries } from "@/queries"
import { todoService } from "../services/todo-service"

export const useDeleteTodo = () => {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: todoService.delete,
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: queries.todos._def,
      })
    },
  })
}
```

## Destructuring Convention

**Always** destructure query and mutation results with descriptive, namespaced names. This avoids collisions when multiple hooks are used in the same component.

### Queries

```tsx
// GOOD: named destructuring
const { data: todos, isLoading: isLoadingTodos } = useGetTodos(filters)
const { data: user, isLoading: isLoadingUser } = useGetUser(userId)

// BAD: generic names collide
const { data, isLoading } = useGetTodos(filters)
const { data: userData, isLoading: userLoading } = useGetUser(userId) // inconsistent
```

### Mutations

```tsx
// GOOD: destructure mutate/mutateAsync with descriptive names
const { mutate: createTodo, isPending: isCreating } = useCreateTodo()
const { mutate: deleteTodo, isPending: isDeleting } = useDeleteTodo()

// GOOD: mutateAsync when you need the promise
const { mutateAsync: updateTodo, isPending: isUpdating } = useUpdateTodo()
```

### Full Component Example

```tsx
const TodoPage = ({ todoId }: { todoId: string }) => {
  const { data: todo, isLoading: isLoadingTodo } = useGetTodo(todoId)
  const { mutate: deleteTodo, isPending: isDeleting } = useDeleteTodo()

  if (isLoadingTodo) return <TodoSkeleton />
  if (!todo) return <NotFound />

  return (
    <div>
      <h1>{todo.title}</h1>
      <button onClick={() => deleteTodo(todoId)} disabled={isDeleting}>
        {isDeleting ? "Deleting..." : "Delete"}
      </button>
    </div>
  )
}
```

## Cache Invalidation Patterns

Use the `_def` property from query-key-factory to invalidate at different granularities:

```ts
// Invalidate ALL todo queries (list, detail, everything)
queryClient.invalidateQueries({ queryKey: queries.todos._def })

// Invalidate only list queries (all filter variants)
queryClient.invalidateQueries({ queryKey: queries.todos.list._def })

// Invalidate a specific detail query
queryClient.invalidateQueries({ queryKey: queries.todos.detail(todoId).queryKey })
```

## Conditional Queries

Use the `enabled` option. Spread the query key object and add `enabled`:

```ts
export const useGetTodo = (todoId: string | undefined) => {
  return useQuery({
    ...queries.todos.detail(todoId!),
    enabled: !!todoId,
  })
}
```

## Rules

1. **Never** use raw string arrays as query keys — always use `queries.*` from the merged factory
2. **Never** put all keys in a single file — one `createQueryKeys` per feature
3. **Never** combine multiple queries/mutations in one hook — one hook per operation
4. **Always** destructure with named aliases: `{ data: todos, isLoading: isLoadingTodos }`
5. **Always** invalidate via `_def` for broad invalidation, `.queryKey` for specific
6. **Always** colocate query key files with their feature: `features/<name>/queries/<name>-keys.ts`
7. **Always** colocate hooks with their feature: `features/<name>/hooks/use-<action>-<name>.ts`
