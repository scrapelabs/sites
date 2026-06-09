import { Router } from "express";
import { getAuth, createClerkClient } from "@clerk/express";
import { z } from "zod";
import { supportMessages } from "../store";

const router = Router();

const clerkClient = createClerkClient({ secretKey: process.env.CLERK_SECRET_KEY });

const submitSchema = z.object({
  subject: z.string().min(1).max(200),
  body: z.string().min(1).max(4000),
});

router.post("/support/message", async (req, res) => {
  const auth = getAuth(req);
  if (!auth?.userId) {
    res.status(401).json({ error: "Unauthorized" });
    return;
  }

  const parsed = submitSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: "Invalid input", detail: parsed.error.flatten() });
    return;
  }

  let userEmail = "";
  let userName = "";
  try {
    const user = await clerkClient.users.getUser(auth.userId);
    userEmail = user.emailAddresses.find((e) => e.id === user.primaryEmailAddressId)?.emailAddress ?? "";
    userName = [user.firstName, user.lastName].filter(Boolean).join(" ") || userEmail.split("@")[0];
  } catch {
    // fallback — still accept the message
  }

  const id = `msg_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
  const msg = {
    id,
    userId: auth.userId,
    userEmail,
    userName,
    subject: parsed.data.subject,
    body: parsed.data.body,
    createdAt: new Date().toISOString(),
    status: "open" as const,
  };

  supportMessages.unshift(msg);
  res.status(201).json({ id, status: "open" });
});

router.get("/support/messages/mine", async (req, res) => {
  const auth = getAuth(req);
  if (!auth?.userId) {
    res.status(401).json({ error: "Unauthorized" });
    return;
  }
  const mine = supportMessages.filter((m) => m.userId === auth.userId);
  res.json(mine);
});

export default router;
