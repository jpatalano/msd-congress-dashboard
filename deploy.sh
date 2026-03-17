#!/bin/bash
# MSD Congress Activity Dashboard — Deploy Script
# Pushes latest code to GitHub then triggers Railway redeploy on the SAME service.
# Usage: bash deploy.sh "commit message"
# Usage (staging): bash deploy.sh "commit message" staging

set -e

RAILWAY_TOKEN="9f4a55a2-8aff-4e2b-afb7-13a1549e8f4e"
PROJECT_ID="410f32c8-7679-4572-b807-cb5215b00e5a"
ENV_ID="adf3963e-554e-4317-b338-d0c07e185934"
RAILWAY_URL="https://ingenious-creation-production-721f.up.railway.app"
PROD_URL="https://docs.incadence.com/msd/activityDashboard"

# Staging env (set these once staging service is created)
STAGING_ENV_ID=""
STAGING_SERVICE_ID=""

TARGET="${2:-production}"
MSG="${1:-Update dashboard}"

# Pick service based on target
if [ "$TARGET" = "staging" ]; then
  if [ -z "$STAGING_SERVICE_ID" ] || [ -z "$STAGING_ENV_ID" ]; then
    echo "✗ Staging service not yet configured. Edit deploy.sh to set STAGING_SERVICE_ID and STAGING_ENV_ID."
    exit 1
  fi
  SERVICE_ID="$STAGING_SERVICE_ID"
  DEPLOY_ENV_ID="$STAGING_ENV_ID"
  echo "==> Deploying to STAGING..."
else
  SERVICE_ID="b638ab97-dea6-4c1c-a2e2-2385007579bd"
  DEPLOY_ENV_ID="$ENV_ID"
  echo "==> Deploying to PRODUCTION..."
fi

echo "==> Committing and pushing to GitHub..."
git add -A
git commit -m "$MSG" || echo "(nothing to commit)"
GIT_ASKPASS=/bin/true git push origin master

echo "==> Triggering Railway redeploy on service $SERVICE_ID..."
RESULT=$(curl -s -X POST https://backboard.railway.app/graphql/v2 \
  -H "Authorization: Bearer $RAILWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"query\":\"mutation { serviceInstanceDeploy(serviceId: \\\"$SERVICE_ID\\\", environmentId: \\\"$DEPLOY_ENV_ID\\\") }\"}")

echo "  Trigger result: $RESULT"

if echo "$RESULT" | grep -q '"serviceInstanceDeploy":true'; then
  echo "  Railway deploy triggered successfully!"
else
  echo "  ⚠ Deploy trigger may have failed. Check Railway dashboard."
fi

echo "==> Waiting for deployment..."
for i in $(seq 1 30); do
  sleep 10
  STATUS=$(curl -s -X POST https://backboard.railway.app/graphql/v2 \
    -H "Authorization: Bearer $RAILWAY_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"query\":\"{ deployments(input: { serviceId: \\\"$SERVICE_ID\\\" }) { edges { node { id status createdAt } } } }\"}" \
    | python3 -c "
import json,sys
d=json.load(sys.stdin)
deps=d.get('data',{}).get('deployments',{}).get('edges',[])
if deps:
  n=deps[0]['node']
  print(n['status'], n['id'][:8])
else:
  print('PENDING')
")
  echo "  [$i] $STATUS"
  if echo "$STATUS" | grep -q "SUCCESS"; then
    echo ""
    echo "✓ Deployed successfully!"
    echo "  Railway: $RAILWAY_URL"
    echo "  Production: $PROD_URL"
    exit 0
  fi
  if echo "$STATUS" | grep -q "FAILED\|CRASHED"; then
    echo "✗ Deployment failed. Check Railway logs."
    exit 1
  fi
done

echo "Timed out waiting for deployment."
exit 1
