FROM golang:1.26-alpine AS build
WORKDIR /src
COPY go.mod main.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /shortlink .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /shortlink /shortlink
EXPOSE 8080
ENTRYPOINT ["/shortlink"]
