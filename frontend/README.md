# OrbitOps Assistant frontend

React (Vite) chat app, served in production by nginx (`nginx.conf`), which
also routes the API: `/api/auth/` and `/api/admin/` to the auth service,
`/api/ops/` to the ops service and the rest of `/api/` to the chat service.

```bash
npm install
npm run dev        # http://localhost:5173, talks to the backend on localhost:8000
npm run build      # production build into dist/
```

Set `VITE_API_URL` if the backend isn't on `localhost:8000`. The container
build sets it to an empty string so the app calls `/api/...` on its own
origin. See the main [README](../README.md) for the whole project.
