import http from "k6/http";
import { check, sleep } from "k6";

const scenarioName = __ENV.SCENARIO || "load";
const baseUrl = (__ENV.BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const workloadProfile = __ENV.WORKLOAD_PROFILE || "public-catalog";
const userToken = __ENV.USER_TOKEN || __ENV.AUTH_TOKEN || "";
const merchantToken = __ENV.MERCHANT_TOKEN || "";
const adminToken = __ENV.ADMIN_TOKEN || "";

const scenarios = {
  load: {
    executor: "constant-vus",
    vus: Number(__ENV.LOAD_VUS || 25),
    duration: __ENV.LOAD_DURATION || "2m",
  },
  stress: {
    executor: "ramping-vus",
    startVUs: 10,
    stages: [
      { duration: "1m", target: Number(__ENV.STRESS_VUS || 75) },
      { duration: "2m", target: Number(__ENV.STRESS_VUS || 75) },
      { duration: "1m", target: 0 },
    ],
  },
  spike: {
    executor: "ramping-vus",
    startVUs: 5,
    stages: [
      { duration: "15s", target: Number(__ENV.SPIKE_VUS || 150) },
      { duration: "45s", target: Number(__ENV.SPIKE_VUS || 150) },
      { duration: "30s", target: 5 },
    ],
  },
  soak: {
    executor: "constant-vus",
    vus: Number(__ENV.SOAK_VUS || 20),
    duration: __ENV.SOAK_DURATION || "30m",
  },
};

if (!Object.prototype.hasOwnProperty.call(scenarios, scenarioName)) {
  throw new Error(`unsupported SCENARIO: ${scenarioName}`);
}

export const options = {
  scenarios: { [scenarioName]: scenarios[scenarioName] },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<1000", "p(99)<2000"],
    "http_req_duration{group:health}": ["p(95)<50", "p(99)<100"],
    "http_req_duration{group:catalog}": ["p(95)<300", "p(99)<800"],
    "http_req_duration{group:user_order}": ["p(95)<400", "p(99)<1000"],
    "http_req_duration{group:user_workspace}": ["p(95)<500", "p(99)<1200"],
    "http_req_duration{group:messaging}": ["p(95)<500", "p(99)<1200"],
    "http_req_duration{group:merchant_workspace}": ["p(95)<600", "p(99)<1500"],
    "http_req_duration{group:admin_workspace}": ["p(95)<700", "p(99)<1800"],
    checks: ["rate>0.99"],
  },
};

function get(path, group, headers = {}) {
  const response = http.get(`${baseUrl}${path}`, {
    headers,
    tags: { group, scenario: scenarioName, workload: workloadProfile },
    timeout: "5s",
  });
  check(response, {
    "request returns 200": (result) => result.status === 200,
  });
}

function bearer(token) {
  return { Authorization: `Bearer ${token}` };
}

function choose(paths) {
  return paths[Math.floor(Math.random() * paths.length)];
}

function publicCatalogJourney() {
  const choices = ["/api/v1/homepage", "/api/v1/products"];
  if (__ENV.PRODUCT_ID) {
    choices.push(`/api/v1/products/${__ENV.PRODUCT_ID}`);
    choices.push(`/api/v1/products/${__ENV.PRODUCT_ID}/reviews`);
  }
  if (__ENV.STORE_ID) choices.push(`/api/v1/stores/${__ENV.STORE_ID}`);
  get(choose(choices), "catalog");
}

function userWorkspace() {
  if (!userToken) throw new Error("USER_TOKEN is required for user-workspace");
  const choices = [
    "/api/v1/users/me/dashboard",
    "/api/v1/users/me/cart",
    "/api/v1/users/me/orders",
    "/api/v1/conversations?limit=30",
  ];
  if (__ENV.ORDER_ID) choices.push(`/api/v1/orders/${__ENV.ORDER_ID}`);
  if (__ENV.CONVERSATION_ID) {
    choices.push(`/api/v1/conversations/${__ENV.CONVERSATION_ID}/messages?limit=50`);
  }
  get(choose(choices), "user_workspace", bearer(userToken));
}

function messagingRead() {
  if (!userToken || !__ENV.CONVERSATION_ID) {
    throw new Error("USER_TOKEN and CONVERSATION_ID are required for messaging-read");
  }
  get(
    `/api/v1/conversations/${__ENV.CONVERSATION_ID}/messages?limit=50`,
    "messaging",
    bearer(userToken),
  );
}

function merchantWorkspace() {
  if (!merchantToken) throw new Error("MERCHANT_TOKEN is required for merchant-workspace");
  const choices = [
    "/api/v1/merchant/account/security",
    "/api/v1/admin/products?limit=50",
    "/api/v1/admin/orders?limit=50",
    "/api/v1/merchant/support/exclusive-conversation/messages?limit=50",
  ];
  get(choose(choices), "merchant_workspace", bearer(merchantToken));
}

function adminWorkspace() {
  if (!adminToken) throw new Error("ADMIN_TOKEN is required for admin-workspace");
  const choices = [
    "/api/v1/admin/dashboard",
    "/api/v1/admin/users?limit=50",
    "/api/v1/admin/stores?limit=50",
    "/api/v1/support/human-service-tickets?limit=50",
    "/api/v1/admin/ai/runs/provider-health",
  ];
  get(choose(choices), "admin_workspace", bearer(adminToken));
}

export default function () {
  if (workloadProfile === "health") {
    get("/health/live", "health");
  } else if (workloadProfile === "public-catalog") {
    publicCatalogJourney();
  } else if (workloadProfile === "user-order-read") {
    if (!userToken || !__ENV.ORDER_ID) {
      throw new Error("USER_TOKEN and ORDER_ID are required for user-order-read");
    }
    const choices = ["/api/v1/users/me/orders", `/api/v1/orders/${__ENV.ORDER_ID}`];
    get(choose(choices), "user_order", bearer(userToken));
  } else if (workloadProfile === "user-workspace") {
    userWorkspace();
  } else if (workloadProfile === "messaging-read") {
    messagingRead();
  } else if (workloadProfile === "merchant-workspace") {
    merchantWorkspace();
  } else if (workloadProfile === "admin-workspace") {
    adminWorkspace();
  } else if (workloadProfile === "mixed-read") {
    const available = [publicCatalogJourney];
    if (userToken) available.push(userWorkspace);
    if (merchantToken) available.push(merchantWorkspace);
    if (adminToken) available.push(adminWorkspace);
    choose(available)();
  } else {
    throw new Error(`unsupported WORKLOAD_PROFILE: ${workloadProfile}`);
  }
  sleep(Number(__ENV.THINK_TIME_SECONDS || 0.2));
}
