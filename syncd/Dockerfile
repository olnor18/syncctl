FROM golang:1.18-alpine AS build
RUN apk --no-cache add git
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY main.go ./
RUN CGO_ENABLED=0 go build -a .

FROM alpine:3.15
RUN apk --no-cache add ca-certificates skopeo
WORKDIR /
COPY --from=build /app/syncd ./
CMD ["/syncd"]
