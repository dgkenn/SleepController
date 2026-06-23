/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    const apiUrl = process.env.API_URL || 'http://localhost:8000';
    // Strip the /api prefix so requests reach the FastAPI routes mounted at root
    // (e.g. /api/auth/login -> {apiUrl}/auth/login). This mirrors Caddy's
    // `handle_path /api/*` behaviour so standalone `next start` works identically.
    return [
      {
        source: '/api/:path*',
        destination: `${apiUrl}/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
