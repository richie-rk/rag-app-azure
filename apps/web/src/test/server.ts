// The MSW server every test shares. The setup file starts it so that any unplanned request fails a
// test (10-web-client.md section 13.1). Tests add handlers with `server.use(...)`.

import { setupServer } from "msw/node";

import { defaultHandlers } from "./handlers";

export const server = setupServer(...defaultHandlers);
