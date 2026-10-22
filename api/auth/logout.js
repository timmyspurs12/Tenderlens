// Explicit route file: /api/auth/<name> must be reachable even when the
// bracket catch-all is not bound by the filesystem router.
import handler from '../_gateway.js';

export default handler;
