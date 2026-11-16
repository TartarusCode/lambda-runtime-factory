package main

import (
	"context"
	"github.com/aws/aws-lambda-go/lambda"
)

type Event struct {
	Message string `json:"message"`
}

type Response struct {
	StatusCode int    `json:"statusCode"`
	Body       string `json:"body"`
}

func handler(ctx context.Context, event Event) (Response, error) {
	return Response{StatusCode: 200, Body: `{"message":"hello world"}`}, nil
}

func main() {
	lambda.Start(handler)
}
