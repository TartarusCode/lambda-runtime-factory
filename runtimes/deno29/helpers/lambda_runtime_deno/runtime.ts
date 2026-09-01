const RUNTIME_API = process.env.AWS_LAMBDA_RUNTIME_API!;
const HANDLER = process.env._HANDLER!;
const BASE = `http://${RUNTIME_API}/2018-06-01/runtime`;

const [modulePath, funcName] = HANDLER.includes(".")
  ? [HANDLER.substring(0, HANDLER.lastIndexOf(".")), HANDLER.substring(HANDLER.lastIndexOf(".") + 1)]
  : [HANDLER, "handler"];

const taskRoot = process.env.LAMBDA_TASK_ROOT!;
const moduleFile = /\.(ts|tsx|js|mjs|mts)$/.test(modulePath)
  ? `${taskRoot}/${modulePath}`
  : `${taskRoot}/${modulePath}.ts`;

const mod = await import(moduleFile);
const handlerFn = mod[funcName] ?? mod.default;
if (typeof handlerFn !== "function") {
  const body = JSON.stringify({
    errorMessage: `Handler '${HANDLER}' is not a function`,
    errorType: "Runtime.HandlerNotFound",
  });
  await fetch(`${BASE}/init/error`, {
    method: "POST",
    body,
    headers: { "Content-Type": "application/json" },
  });
  Deno.exit(1);
}

while (true) {
  const next = await fetch(`${BASE}/invocation/next`);
  const requestId = next.headers.get("lambda-runtime-aws-request-id")!;
  const event = await next.json();

  const context = {
    awsRequestId: requestId,
    functionName: process.env.AWS_LAMBDA_FUNCTION_NAME,
    functionVersion: process.env.AWS_LAMBDA_FUNCTION_VERSION,
    invokedFunctionArn: next.headers.get("lambda-runtime-invoked-function-arn"),
    memoryLimitInMB: process.env.AWS_LAMBDA_FUNCTION_MEMORY_SIZE,
    logGroupName: process.env.AWS_LAMBDA_LOG_GROUP_NAME,
    logStreamName: process.env.AWS_LAMBDA_LOG_STREAM_NAME,
    getRemainingTimeInMillis: () => {
      const deadline = next.headers.get("lambda-runtime-deadline-ms");
      return deadline ? Math.max(0, Number(deadline) - Date.now()) : 0;
    },
  };

  try {
    const result = await handlerFn(event, context);
    const body = typeof result === "string" ? result : JSON.stringify(result);
    await fetch(`${BASE}/invocation/${requestId}/response`, {
      method: "POST",
      body,
      headers: { "Content-Type": "application/json" },
    });
  } catch (err: unknown) {
    const error = err as Error;
    const body = JSON.stringify({
      errorMessage: error.message,
      errorType: error.name,
      stackTrace: error.stack?.split("\n"),
    });
    await fetch(`${BASE}/invocation/${requestId}/error`, {
      method: "POST",
      body,
      headers: { "Content-Type": "application/json" },
    });
  }
}
