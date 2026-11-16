// Start-up (10-web-client.md section 4.2). Zod's jitless setup comes first, before any schema
// is built.
import "./bootstrap/zodSetup";

import { start } from "./bootstrap/Startup";

const container = document.getElementById("root");
if (container !== null) void start(container);
