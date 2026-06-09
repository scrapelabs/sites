import { Router } from "express";
import { getAuth, createClerkClient } from "@clerk/express";
import { z } from "zod";
import { supportMessages } from "../store";

const router = Router();

const SUPER_ADMIN_EMAIL = "khemiri.mohamed.ensi@gmail.com";

const clerkClient = createClerkClient({
  secretKey: process.env.CLERK_SECRET_KEY,
});

const mockPurchases: {
  id: string;
  userId: string;
  userEmail: string;
  userName: string;
  planId: string;
  planName: string;
  planType: string;
  amount: number;
  status: "active" | "cancelled" | "expired";
  createdAt: string;
  expiresAt: string;
}[] = [
  {
    id: "ord_001",
    userId: "demo_user_1",
    userEmail: "alice@example.com",
    userName: "Alice Johnson",
    planId: "residential-pro",
    planName: "Professional Residential",
    planType: "residential",
    amount: 29.99,
    status: "active",
    createdAt: new Date(Date.now() - 15 * 24 * 60 * 60 * 1000).toISOString(),
    expiresAt: new Date(Date.now() + 15 * 24 * 60 * 60 * 1000).toISOString(),
  },
  {
    id: "ord_002",
    userId: "demo_user_2",
    userEmail: "bob@techcorp.io",
    userName: "Bob Martinez",
    planId: "datacenter-pro",
    planName: "Professional Datacenter",
    planType: "datacenter",
    amount: 19.99,
    status: "active",
    createdAt: new Date(Date.now() - 8 * 24 * 60 * 60 * 1000).toISOString(),
    expiresAt: new Date(Date.now() + 22 * 24 * 60 * 60 * 1000).toISOString(),
  },
  {
    id: "ord_003",
    userId: "demo_user_3",
    userEmail: "carol@datascrape.net",
    userName: "Carol Wu",
    planId: "residential-business",
    planName: "Business Residential",
    planType: "residential",
    amount: 79.99,
    status: "active",
    createdAt: new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString(),
    expiresAt: new Date(Date.now() + 27 * 24 * 60 * 60 * 1000).toISOString(),
  },
  {
    id: "ord_004",
    userId: "demo_user_4",
    userEmail: "david@automate.co",
    userName: "David Kim",
    planId: "ipv6-pro",
    planName: "Professional IPv6",
    planType: "ipv6",
    amount: 14.99,
    status: "expired",
    createdAt: new Date(Date.now() - 40 * 24 * 60 * 60 * 1000).toISOString(),
    expiresAt: new Date(Date.now() - 10 * 24 * 60 * 60 * 1000).toISOString(),
  },
  {
    id: "ord_005",
    userId: "demo_user_5",
    userEmail: "emma@reseller.pro",
    userName: "Emma Davis",
    planId: "residential-enterprise",
    planName: "Enterprise Residential",
    planType: "residential",
    amount: 199.99,
    status: "active",
    createdAt: new Date(Date.now() - 1 * 24 * 60 * 60 * 1000).toISOString(),
    expiresAt: new Date(Date.now() + 29 * 24 * 60 * 60 * 1000).toISOString(),
  },
  {
    id: "ord_006",
    userId: "demo_user_6",
    userEmail: "frank@proxytool.dev",
    userName: "Frank Nguyen",
    planId: "datacenter-starter",
    planName: "Starter Datacenter",
    planType: "datacenter",
    amount: 7.99,
    status: "cancelled",
    createdAt: new Date(Date.now() - 20 * 24 * 60 * 60 * 1000).toISOString(),
    expiresAt: new Date(Date.now() - 5 * 24 * 60 * 60 * 1000).toISOString(),
  },
  {
    id: "ord_007",
    userId: "demo_user_2",
    userEmail: "bob@techcorp.io",
    userName: "Bob Martinez",
    planId: "residential-starter",
    planName: "Starter Residential",
    planType: "residential",
    amount: 9.99,
    status: "expired",
    createdAt: new Date(Date.now() - 65 * 24 * 60 * 60 * 1000).toISOString(),
    expiresAt: new Date(Date.now() - 35 * 24 * 60 * 60 * 1000).toISOString(),
  },
];

async function requireSuperAdmin(req: any, res: any): Promise<boolean> {
  const auth = getAuth(req);
  if (!auth?.userId) {
    res.status(401).json({ error: "Unauthorized" });
    return false;
  }
  try {
    const user = await clerkClient.users.getUser(auth.userId);
    const primaryEmail = user.emailAddresses.find(
      (e) => e.id === user.primaryEmailAddressId
    )?.emailAddress;
    if (primaryEmail !== SUPER_ADMIN_EMAIL) {
      res.status(403).json({ error: "Forbidden" });
      return false;
    }
    return true;
  } catch {
    res.status(401).json({ error: "Unauthorized" });
    return false;
  }
}

router.get("/admin/users", async (req, res) => {
  if (!(await requireSuperAdmin(req, res))) return;
  try {
    const { data: users } = await clerkClient.users.getUserList({ limit: 100 });
    const formatted = users.map((u) => ({
      id: u.id,
      email: u.emailAddresses.find((e) => e.id === u.primaryEmailAddressId)?.emailAddress || "",
      name: [u.firstName, u.lastName].filter(Boolean).join(" ") || "—",
      createdAt: new Date(u.createdAt).toISOString(),
      lastSignIn: u.lastSignInAt ? new Date(u.lastSignInAt).toISOString() : null,
      banned: u.banned,
    }));
    res.json(formatted);
  } catch (err: any) {
    res.status(500).json({ error: "Failed to fetch users", detail: err.message });
  }
});

router.get("/admin/purchases", async (req, res) => {
  if (!(await requireSuperAdmin(req, res))) return;
  res.json(mockPurchases);
});

router.get("/admin/stats", async (req, res) => {
  if (!(await requireSuperAdmin(req, res))) return;
  try {
    const { data: users } = await clerkClient.users.getUserList({ limit: 500 });
    const active = mockPurchases.filter((p) => p.status === "active");
    const mrr = active.reduce((sum, p) => sum + p.amount, 0);
    const revenue = mockPurchases
      .filter((p) => p.status !== "cancelled")
      .reduce((sum, p) => sum + p.amount, 0);
    res.json({
      totalUsers: users.length,
      activeSubscriptions: active.length,
      mrr: parseFloat(mrr.toFixed(2)),
      totalRevenue: parseFloat(revenue.toFixed(2)),
      totalOrders: mockPurchases.length,
      planBreakdown: {
        residential: mockPurchases.filter((p) => p.planType === "residential" && p.status === "active").length,
        datacenter: mockPurchases.filter((p) => p.planType === "datacenter" && p.status === "active").length,
        ipv6: mockPurchases.filter((p) => p.planType === "ipv6" && p.status === "active").length,
      },
    });
  } catch (err: any) {
    res.status(500).json({ error: "Failed to fetch stats", detail: err.message });
  }
});

router.get("/admin/messages", async (req, res) => {
  if (!(await requireSuperAdmin(req, res))) return;
  res.json(supportMessages);
});

const replySchema = z.object({
  body: z.string().min(1).max(4000),
});

router.post("/admin/messages/:id/reply", async (req, res) => {
  if (!(await requireSuperAdmin(req, res))) return;
  const msg = supportMessages.find((m) => m.id === req.params.id);
  if (!msg) {
    res.status(404).json({ error: "Message not found" });
    return;
  }
  const parsed = replySchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: "Invalid input" });
    return;
  }
  msg.reply = { body: parsed.data.body, createdAt: new Date().toISOString() };
  msg.status = "replied";
  res.json(msg);
});

router.patch("/admin/messages/:id/status", async (req, res) => {
  if (!(await requireSuperAdmin(req, res))) return;
  const msg = supportMessages.find((m) => m.id === req.params.id);
  if (!msg) {
    res.status(404).json({ error: "Message not found" });
    return;
  }
  const { status } = req.body;
  if (!["open", "replied", "closed"].includes(status)) {
    res.status(400).json({ error: "Invalid status" });
    return;
  }
  msg.status = status;
  res.json(msg);
});

export default router;
