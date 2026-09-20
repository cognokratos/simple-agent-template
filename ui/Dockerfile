# syntax=docker/dockerfile:1.7

FROM node:22-alpine

WORKDIR /app

# Dependencies first, so editing application source does not reinstall them.
# `npm ci` installs strictly from package-lock.json and fails if the lockfile and
# package.json disagree, which is what makes an image rebuild reproducible rather
# than dependent on when it ran. The cache mount only keeps the npm download
# cache warm; it cannot change which versions are selected.
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm,sharing=locked npm ci

COPY . .
RUN npm run build

EXPOSE 3000

CMD ["npm", "start"]
