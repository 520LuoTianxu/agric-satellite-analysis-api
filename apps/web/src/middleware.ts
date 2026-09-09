import createMiddleware from "next-intl/middleware";
import { routing } from "./i18n/routing";

export default createMiddleware(routing);

export const config = {
    // Match all paths except api, v1 (API rewrite proxy), _next, static files
    matcher: ["/((?!api|v1|_next|_vercel|.*\\..*).*)"],
};
