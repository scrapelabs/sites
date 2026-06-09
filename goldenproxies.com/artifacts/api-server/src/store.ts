export type SupportMessage = {
  id: string;
  userId: string;
  userEmail: string;
  userName: string;
  subject: string;
  body: string;
  createdAt: string;
  status: "open" | "replied" | "closed";
  reply?: {
    body: string;
    createdAt: string;
  };
};

export const supportMessages: SupportMessage[] = [
  {
    id: "msg_001",
    userId: "demo_user_1",
    userEmail: "alice@example.com",
    userName: "Alice Johnson",
    subject: "How do I rotate proxies automatically?",
    body: "Hi, I'm on the Pro residential plan and I want to know if there's a way to rotate IPs automatically on every request, or do I need to handle that from my code?",
    createdAt: new Date(Date.now() - 2 * 24 * 60 * 60 * 1000).toISOString(),
    status: "open",
  },
  {
    id: "msg_002",
    userId: "demo_user_2",
    userEmail: "bob@techcorp.io",
    userName: "Bob Martinez",
    subject: "Billing question — charged twice",
    body: "I think I was charged twice for my last renewal. My invoice shows two charges of $19.99 on the same day. Can you look into this?",
    createdAt: new Date(Date.now() - 5 * 24 * 60 * 60 * 1000).toISOString(),
    status: "replied",
    reply: {
      body: "Hi Bob, I looked into your account and you're correct — there was a duplicate charge due to a retry on our payment processor. I've issued a full refund for the duplicate amount. It should appear in 3–5 business days. Sorry for the inconvenience!",
      createdAt: new Date(Date.now() - 4 * 24 * 60 * 60 * 1000).toISOString(),
    },
  },
  {
    id: "msg_003",
    userId: "demo_user_3",
    userEmail: "carol@datascrape.net",
    userName: "Carol Wu",
    subject: "Need whitelist for my server IP",
    body: "We're running a scraping cluster at IP 203.0.113.42. Can you whitelist this so we don't need to authenticate every time?",
    createdAt: new Date(Date.now() - 1 * 24 * 60 * 60 * 1000).toISOString(),
    status: "open",
  },
];
