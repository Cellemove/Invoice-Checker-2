import { createClient } from '@supabase/supabase-js'

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY

// Auth is disconnected for this internal tool, so Supabase is optional. When the
// env vars are absent we export a harmless stub exposing just enough of the auth
// surface for ./api.js (getSession) and the dormant ./components/Auth.jsx. To
// re-enable real auth, set VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY.
function makeStub() {
  const noSession = async () => ({ data: { session: null }, error: null })
  const notConfigured = async () => ({
    data: { session: null, user: null },
    error: new Error('Supabase auth is not configured (auth is disconnected).'),
  })
  return {
    auth: {
      getSession: noSession,
      onAuthStateChange: () => ({ data: { subscription: { unsubscribe() {} } } }),
      signInWithPassword: notConfigured,
      signUp: notConfigured,
      signOut: async () => ({ error: null }),
    },
  }
}

export const supabase =
  supabaseUrl && supabaseAnonKey
    ? createClient(supabaseUrl, supabaseAnonKey, {
        auth: {
          persistSession: true,
          autoRefreshToken: true,
          detectSessionInUrl: true,
        },
      })
    : makeStub()
