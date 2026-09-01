export const handler = async (event: unknown, context: unknown) => {
  return {
    statusCode: 200,
    body: JSON.stringify({ message: "hello world" }),
  };
};
