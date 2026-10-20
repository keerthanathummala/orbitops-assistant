FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# Empty API URL -> the app calls /api/... on its own origin (see api.js).
ENV VITE_API_URL=""
RUN npm run build

FROM nginx:1.27-alpine
# Templates are filled in from the environment when the container starts.
COPY nginx.conf /etc/nginx/templates/default.conf.template
# Where the API services are (compose service names; Kubernetes overrides
# these in k8s/21-frontend.yaml), and use the container's own DNS server.
ENV AUTH_UPSTREAM=auth:8000 OPS_UPSTREAM=ops:8000 CHAT_UPSTREAM=backend:8000 \
    NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
