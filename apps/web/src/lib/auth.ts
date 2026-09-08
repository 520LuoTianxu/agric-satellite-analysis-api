import GoogleProvider from "next-auth/providers/google";
import CredentialsProvider from "next-auth/providers/credentials";
import { upsertUser } from "@/lib/db";
import { logger } from "@/lib/logger";
import { DEMO_PROVIDER_ID, isDemoLoginServerEnabled } from "@/lib/demo-login";

import type { NextAuthOptions } from "next-auth";

const DEMO_USER = {
    email: "demo@agric-satellite-analysis.local",
    name: "Demo",
    image: null as string | null,
};

const providers: NextAuthOptions["providers"] = [
    GoogleProvider({
        clientId: process.env.GOOGLE_CLIENT_ID ?? "",
        clientSecret: process.env.GOOGLE_CLIENT_SECRET ?? "",
    }),
];

if (isDemoLoginServerEnabled()) {
    providers.push(
        CredentialsProvider({
            id: DEMO_PROVIDER_ID,
            name: "Demo",
            credentials: {
                login: { label: "Demo", type: "text" },
            },
            async authorize() {
                if (!isDemoLoginServerEnabled()) {
                    return null;
                }
                return {
                    id: DEMO_PROVIDER_ID,
                    email: DEMO_USER.email,
                    name: DEMO_USER.name,
                    image: DEMO_USER.image,
                };
            },
        }),
    );
}

export const authOptions: NextAuthOptions = {
    providers,
    callbacks: {
        async signIn({ user }) {
            try {
                const dbUser = await upsertUser(
                    user.email!,
                    user.name || user.email!,
                    user.image,
                );
                (user as any).dbId = dbUser.id;
                logger.info({ email: user.email }, "user_signed_in");
                return true;
            } catch (error) {
                logger.error({ error, email: user.email }, "user_signin_failed");
                return false;
            }
        },
        async jwt({ token, user }) {
            if (user) {
                token.dbId = (user as any).dbId;
            }
            return token;
        },
        async session({ session, token }) {
            if (session.user) {
                (session.user as any).id = token.dbId;
            }
            return session;
        },
    },
    session: {
        strategy: "jwt",
        maxAge: 30 * 24 * 60 * 60, // 30 days
    },
    pages: {
        signIn: "/",
        error: "/",
    },
};
