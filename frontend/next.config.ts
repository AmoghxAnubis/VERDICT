import type { NextConfig } from "next";

const config: NextConfig = {
  poweredByHeader: false,
  async rewrites() {
    const backend = process.env.VERDICT_API_URL || "http://127.0.0.1:8000";
    return [{ source: "/api/verdict/:path*", destination: `${backend}/v1/:path*` }];
  },
};
export default config;
