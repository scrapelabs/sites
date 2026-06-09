import { Router } from "express";

const router = Router();

const plans = [
  // ── Residential Proxies ──────────────────────────────────────────────────
  {
    id: "residential-starter",
    name: "Starter",
    price: 9.99,
    bandwidth: "5 GB",
    locations: 80,
    features: [
      "Real residential IPs",
      "HTTP/HTTPS support",
      "Rotating sessions",
      "24/7 support",
      "195+ countries",
    ],
    popular: false,
    type: "residential",
  },
  {
    id: "residential-pro",
    name: "Professional",
    price: 29.99,
    bandwidth: "25 GB",
    locations: 150,
    features: [
      "Real residential IPs",
      "HTTP/HTTPS/SOCKS5",
      "Rotating & sticky sessions",
      "Sub-user management",
      "API access",
      "Priority support",
      "195+ countries",
    ],
    popular: true,
    type: "residential",
  },
  {
    id: "residential-business",
    name: "Business",
    price: 79.99,
    bandwidth: "100 GB",
    locations: 200,
    features: [
      "Real residential IPs",
      "All protocols",
      "Rotating & sticky sessions",
      "Unlimited sub-users",
      "API access",
      "Dedicated account manager",
      "SLA guarantee",
      "200+ countries",
    ],
    popular: false,
    type: "residential",
  },
  {
    id: "residential-enterprise",
    name: "Enterprise",
    price: 199.99,
    bandwidth: "Unlimited",
    locations: 200,
    features: [
      "Real residential IPs",
      "All protocols",
      "Custom IP pools",
      "White-label option",
      "API access",
      "Dedicated 24/7 support",
      "Custom SLA",
      "200+ countries",
    ],
    popular: false,
    type: "residential",
  },

  // ── IPv6 Proxies ─────────────────────────────────────────────────────────
  {
    id: "ipv6-starter",
    name: "Starter",
    price: 4.99,
    bandwidth: "1,000 IPs",
    locations: 30,
    features: [
      "Dedicated IPv6 addresses",
      "HTTP/HTTPS support",
      "Instant activation",
      "24/7 support",
      "30+ countries",
    ],
    popular: false,
    type: "ipv6",
  },
  {
    id: "ipv6-pro",
    name: "Professional",
    price: 14.99,
    bandwidth: "5,000 IPs",
    locations: 60,
    features: [
      "Dedicated IPv6 addresses",
      "HTTP/HTTPS/SOCKS5",
      "Instant activation",
      "API management",
      "Custom subnets",
      "Priority support",
      "60+ countries",
    ],
    popular: true,
    type: "ipv6",
  },
  {
    id: "ipv6-business",
    name: "Business",
    price: 39.99,
    bandwidth: "20,000 IPs",
    locations: 80,
    features: [
      "Dedicated IPv6 addresses",
      "All protocols",
      "Instant activation",
      "API management",
      "Custom subnets & pools",
      "Dedicated account manager",
      "80+ countries",
    ],
    popular: false,
    type: "ipv6",
  },

  // ── Datacenter Proxies ───────────────────────────────────────────────────
  {
    id: "datacenter-starter",
    name: "Starter",
    price: 7.99,
    bandwidth: "10 GB",
    locations: 20,
    features: [
      "High-speed datacenter IPs",
      "HTTP/HTTPS support",
      "Dedicated IPs",
      "99.9% uptime SLA",
      "24/7 support",
    ],
    popular: false,
    type: "datacenter",
  },
  {
    id: "datacenter-pro",
    name: "Professional",
    price: 19.99,
    bandwidth: "50 GB",
    locations: 40,
    features: [
      "High-speed datacenter IPs",
      "HTTP/HTTPS/SOCKS5",
      "Dedicated & shared pools",
      "API access",
      "99.9% uptime SLA",
      "Priority support",
      "40+ locations",
    ],
    popular: true,
    type: "datacenter",
  },
  {
    id: "datacenter-business",
    name: "Business",
    price: 49.99,
    bandwidth: "200 GB",
    locations: 60,
    features: [
      "High-speed datacenter IPs",
      "All protocols",
      "Dedicated IP pools",
      "API access",
      "Custom IP ranges",
      "99.99% uptime SLA",
      "Dedicated account manager",
      "60+ locations",
    ],
    popular: false,
    type: "datacenter",
  },
];

const stats = {
  totalIPs: "75M+",
  countries: 200,
  uptime: "99.9%",
  successRate: "99.5%",
};

router.get("/proxies/plans", (req, res) => {
  res.json(plans);
});

router.get("/proxies/plans/:id", (req, res) => {
  const plan = plans.find((p) => p.id === req.params.id);
  if (!plan) {
    res.status(404).json({ error: "Plan not found" });
    return;
  }
  res.json(plan);
});

router.get("/proxies/stats", (req, res) => {
  res.json(stats);
});

export default router;
