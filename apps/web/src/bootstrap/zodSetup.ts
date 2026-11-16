// Imported first by main.tsx (10-web-client.md section 3.4): Zod's compiled validators use
// `new Function`, which the site's `script-src 'self'` refuses, so every schema runs jitless.
//
// Every module that imports the generated schemas imports this module first as well. The build
// moves modules shared with lazy screens (Zod and the generated schemas among them) into a chunk
// that the entry chunk imports, and an imported chunk runs before the entry's own code; the first
// import of main.tsx alone would then come after the schemas were built, and Zod's
// `new Function('')` probe would raise a CSP violation on every page load. Imported by the
// schemas' importers, this module lands in their chunk, ahead of the schemas.

import { z } from "zod";

z.config({ jitless: true });
