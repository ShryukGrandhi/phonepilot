#!/usr/bin/env bash
# Deploy the PhonePilot frontend to Vercel, proxying every API/asset path to the backend.
#   BACKEND_URL=https://xxxx.trycloudflare.com bash deploy/vercel/deploy.sh [--prod]
# The pages are copied from src/phonepilot/service/static at deploy time; vercel.json rewrites
# /api/*, /runs/*, /healthz to the backend so cookies stay same-origin (no CORS needed).
set -euo pipefail
cd "$(dirname "$0")"
: "${BACKEND_URL:?set BACKEND_URL (the Mac's public https URL from deploy/mac/expose.sh)}"
BACKEND_URL="${BACKEND_URL%/}"
mkdir -p public
cp ../../src/phonepilot/service/static/app.html public/index.html
cp ../../src/phonepilot/service/static/login.html public/login.html
cat > vercel.json <<JSON
{
  "\$schema": "https://openapi.vercel.sh/vercel.json",
  "cleanUrls": true,
  "rewrites": [
    { "source": "/api/:path*", "destination": "$BACKEND_URL/api/:path*" },
    { "source": "/runs/:path*", "destination": "$BACKEND_URL/runs/:path*" },
    { "source": "/healthz", "destination": "$BACKEND_URL/healthz" }
  ],
  "headers": [
    { "source": "/(.*)", "headers": [
      { "key": "X-Frame-Options", "value": "DENY" },
      { "key": "X-Content-Type-Options", "value": "nosniff" },
      { "key": "Referrer-Policy", "value": "no-referrer" }
    ] }
  ]
}
JSON
echo "backend: $BACKEND_URL"
vercel deploy --yes "$@"
