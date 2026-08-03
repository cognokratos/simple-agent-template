# syntax=docker/dockerfile:1.7

FROM node:22-alpine

WORKDIR /app

# Keep npm downloads between builds. Add and commit package-lock.json, then
# replace `npm install` with `npm ci` for reproducible dependency resolution.
COPY package*.json ./
RUN --mount=type=cache,target=/root/.npm,sharing=locked npm install

COPY . .
RUN npm run build

EXPOSE 3000

CMD ["npm", "start"]
