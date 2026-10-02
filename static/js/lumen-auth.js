const NEON_AUTH_VERSION = '0.5.0-beta';

export async function createLumenAuthClient(config) {
  if (config.provider === 'neon') {
    if (!config.neonAuthUrl) return null;
    const [neon, adapters] = await Promise.all([
      import(`https://esm.sh/@neondatabase/auth@${NEON_AUTH_VERSION}?bundle`),
      import(`https://esm.sh/@neondatabase/auth@${NEON_AUTH_VERSION}/vanilla/adapters?bundle`),
    ]);
    const auth = neon.createAuthClient(config.neonAuthUrl, {
      adapter: adapters.SupabaseAuthAdapter(),
    });
    return {auth};
  }

  if (!config.supabaseUrl || !config.supabaseAnonKey) return null;
  const supabase = await import('https://cdn.jsdelivr.net/npm/@supabase/supabase-js/+esm');
  return supabase.createClient(config.supabaseUrl, config.supabaseAnonKey, config.supabaseOptions);
}
