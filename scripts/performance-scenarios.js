import http from "k6/http";
import { check, sleep } from "k6";

const scenarioName = __ENV.SCENARIO || "load";
const baseUrl = (__ENV.BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const workloadProfile = __ENV.WORKLOAD_PROFILE || "public-catalog";

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

export default function () {
  if (workloadProfile === "health") {
    get("/health/live", "health");
  } else if (workloadProfile === "public-catalog") {
    const choices = ["/api/v1/homepage", "/api/v1/products"];
    if (__ENV.PRODUCT_ID) choices.push(`/api/v1/products/${__ENV.PRODUCT_ID}`);
    if (__ENV.STORE_ID) choices.push(`/api/v1/stores/${__ENV.STORE_ID}`);
    get(choices[Math.floor(Math.random() * choices.length)], "catalog");
  } else if (workloadProfile === "user-order-read") {
    if (!__ENV.AUTH_TOKEN || !__ENV.ORDER_ID) {
      throw new Error("AUTH_TOKEN and ORDER_ID are required for user-order-read");
    }
    const choices = ["/api/v1/users/me/orders", `/api/v1/orders/${__ENV.ORDER_ID}`];
    get(choices[Math.floor(Math.random() * choices.length)], "user_order", {
      Authorization: `Bearer ${__ENV.AUTH_TOKEN}`,
    });
  } else {
    throw new Error(`unsupported WORKLOAD_PROFILE: ${workloadProfile}`);
  }
  sleep(Number(__ENV.THINK_TIME_SECONDS || 0.2));
}
