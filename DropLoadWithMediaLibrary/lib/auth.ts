import { betterAuth } from 'better-auth'
import { Pool } from 'pg'

const origin = (value?: string) => (value ? (value.startsWith('http') ? value : `https://${value}`) : null)
const baseURL = process.env.BETTER_AUTH_URL || origin(process.env.VERCEL_PROJECT_PRODUCTION_URL) || origin(process.env.VERCEL_URL) || process.env.V0_RUNTIME_URL || 'http://localhost:3000'
const trustedOrigins = [
  'http://localhost:3000',
  process.env.V0_RUNTIME_URL,
  process.env.V0_DEV_APP_URL,
  process.env.V0_BUILD_URL,
  process.env.V0_SANDBOX_URL,
  origin(process.env.VERCEL_URL),
  origin(process.env.VERCEL_PROJECT_PRODUCTION_URL),
].filter(Boolean) as string[]

export const auth = betterAuth({
  database: new Pool({ connectionString: process.env.DATABASE_URL }),
  emailAndPassword: { enabled: true, autoSignIn: true },
  baseURL,
  trustedOrigins,
  ...(process.env.NODE_ENV === 'development' ? {
    advanced: { defaultCookieAttributes: { sameSite: 'none' as const, secure: true } },
  } : {}),
})
