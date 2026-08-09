FROM node:22-alpine as build

WORKDIR /app

# Copy package files
COPY package*.json ./

# npm ci：按 lockfile 精确安装（可复现构建）；npm install 会顺手改 lockfile
RUN npm ci

# Copy source code
COPY . .

# Build application
RUN npm run build

# Production stage with nginx
FROM nginx:alpine

# Copy built files
COPY --from=build /app/dist /usr/share/nginx/html

# Copy nginx configuration template. The nginx image renders this with envsubst.
COPY nginx.conf.template /etc/nginx/templates/default.conf.template

EXPOSE 80

# 443 上的 /health 由 nginx 反代到后端：探活同时覆盖"nginx 起着"与"到后端
# 的链路通着"。证书是 LAN 自签，busybox wget 需跳过校验。
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD wget -q --no-check-certificate --spider https://127.0.0.1:443/health || exit 1

CMD ["nginx", "-g", "daemon off;"]
