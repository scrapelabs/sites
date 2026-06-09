import { Router } from "express";
import { SubmitLeadBody } from "@workspace/api-zod";
import { randomUUID } from "crypto";

const router = Router();

const leads: Array<{
  id: string;
  name: string;
  email: string;
  company?: string;
  message: string;
  createdAt: string;
}> = [];

router.post("/leads", (req, res) => {
  const result = SubmitLeadBody.safeParse(req.body);
  if (!result.success) {
    res.status(400).json({ error: "Invalid input", details: result.error });
    return;
  }

  const lead = {
    id: randomUUID(),
    name: result.data.name,
    email: result.data.email,
    company: result.data.company,
    message: result.data.message,
    createdAt: new Date().toISOString(),
  };

  leads.push(lead);
  req.log.info({ leadId: lead.id }, "Lead submitted");
  res.status(201).json(lead);
});

export default router;
