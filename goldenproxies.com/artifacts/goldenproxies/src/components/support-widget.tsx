import React, { useState } from "react";
import { useUser } from "@clerk/react";
import {
  MessageCircle, X, ChevronRight, Mail, FileText,
  HelpCircle, Zap, Send, CheckCircle2, ExternalLink, Loader2
} from "lucide-react";

const API_BASE = import.meta.env.VITE_API_URL ?? "";

const FAQ = [
  {
    q: "How do I get started with proxies?",
    a: "Purchase a plan from the Pricing section in your dashboard, then use the Proxy Generator to create your proxy list.",
  },
  {
    q: "What proxy formats are supported?",
    a: "We support IP:Port:User:Pass, User:Pass@IP:Port, IP:Port only, and User:Pass only — all configurable in the generator.",
  },
  {
    q: "What protocols do you offer?",
    a: "All plans support HTTP and SOCKS5 protocols. You can choose your protocol in the Proxy Generator.",
  },
  {
    q: "How many countries are available?",
    a: "Residential proxies cover 195+ countries. IPv6 covers 80+ and Datacenter covers 60+ locations.",
  },
  {
    q: "What is your uptime guarantee?",
    a: "We guarantee 99.9% network uptime for all proxy types, with 99.99% for Business and Enterprise plans.",
  },
];

type View = "home" | "faq" | "faq-item" | "contact" | "sent";

export function SupportWidget() {
  const { user, isSignedIn } = useUser();
  const [open, setOpen] = useState(false);
  const [view, setView] = useState<View>("home");
  const [activeFaq, setActiveFaq] = useState<number | null>(null);
  const [message, setMessage] = useState("");
  const [subject, setSubject] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState("");

  if (!isSignedIn) return null;

  async function handleSend(e: React.FormEvent) {
    e.preventDefault();
    if (!message.trim()) return;
    setSending(true);
    setSendError("");
    try {
      const res = await fetch(`${API_BASE}/api/support/message`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          subject: subject.trim() || "Support request",
          body: message.trim(),
        }),
      });
      if (!res.ok) throw new Error("Failed to send");
      setView("sent");
      setMessage("");
      setSubject("");
    } catch {
      setSendError("Could not send — please try again.");
    } finally {
      setSending(false);
    }
  }

  function reset() {
    setView("home");
    setSendError("");
  }

  return (
    <>
      {/* Popup panel */}
      {open && (
        <div className="fixed bottom-24 right-6 w-[360px] max-w-[calc(100vw-2rem)] bg-white border border-primary/15 rounded-3xl shadow-2xl shadow-primary/10 z-50 flex flex-col overflow-hidden"
          style={{ maxHeight: "520px" }}>

          {/* Header */}
          <div className="bg-gradient-to-r from-[#C9A227] to-[#D4AF37] px-5 py-4 flex items-center justify-between flex-shrink-0">
            <div className="flex items-center gap-3">
              <div className="w-9 h-9 rounded-xl bg-white/20 flex items-center justify-center">
                <MessageCircle className="w-5 h-5 text-white" />
              </div>
              <div>
                <div className="text-white font-bold text-sm">GoldenProxies Support</div>
                <div className="text-white/80 text-xs flex items-center gap-1.5">
                  <span className="w-1.5 h-1.5 rounded-full bg-green-300 inline-block" />
                  Typically replies in a few hours
                </div>
              </div>
            </div>
            <button onClick={() => setOpen(false)} className="text-white/80 hover:text-white transition-colors">
              <X className="w-5 h-5" />
            </button>
          </div>

          {/* Content */}
          <div className="flex-1 overflow-y-auto">

            {/* Home view */}
            {view === "home" && (
              <div className="p-5 space-y-3">
                <p className="text-sm text-muted-foreground">
                  Hi <span className="font-semibold text-foreground">{user?.firstName || "there"}</span> 👋 How can we help?
                </p>

                <button
                  onClick={() => setView("contact")}
                  className="w-full flex items-center justify-between px-4 py-3.5 rounded-2xl border border-primary/10 bg-primary/3 hover:border-primary hover:bg-primary/8 transition-all group"
                >
                  <div className="flex items-center gap-3">
                    <div className="w-8 h-8 rounded-xl bg-primary/10 flex items-center justify-center">
                      <Send className="w-4 h-4 text-primary" />
                    </div>
                    <div className="text-left">
                      <div className="text-sm font-semibold text-foreground">Send us a message</div>
                      <div className="text-xs text-muted-foreground">We'll reply via email</div>
                    </div>
                  </div>
                  <ChevronRight className="w-4 h-4 text-muted-foreground group-hover:text-primary transition-colors" />
                </button>

                <button
                  onClick={() => setView("faq")}
                  className="w-full flex items-center justify-between px-4 py-3.5 rounded-2xl border border-primary/10 bg-primary/3 hover:border-primary hover:bg-primary/8 transition-all group"
                >
                  <div className="flex items-center gap-3">
                    <div className="w-8 h-8 rounded-xl bg-primary/10 flex items-center justify-center">
                      <HelpCircle className="w-4 h-4 text-primary" />
                    </div>
                    <div className="text-left">
                      <div className="text-sm font-semibold text-foreground">Browse FAQs</div>
                      <div className="text-xs text-muted-foreground">Quick answers to common questions</div>
                    </div>
                  </div>
                  <ChevronRight className="w-4 h-4 text-muted-foreground group-hover:text-primary transition-colors" />
                </button>

                <div className="pt-2 border-t border-primary/8">
                  <p className="text-xs text-muted-foreground mb-3 font-medium uppercase tracking-wider">Quick links</p>
                  <div className="space-y-1">
                    {[
                      { icon: Zap, label: "Proxy Generator", hint: "Build your list" },
                      { icon: FileText, label: "Pricing Plans", hint: "View all plans" },
                      { icon: Mail, label: "Email Support", hint: "support@goldenproxies.com" },
                    ].map((item) => (
                      <div key={item.label} className="flex items-center gap-3 px-3 py-2 rounded-xl hover:bg-primary/5 transition-colors cursor-pointer">
                        <item.icon className="w-4 h-4 text-primary opacity-70 flex-shrink-0" />
                        <div>
                          <div className="text-xs font-semibold text-foreground">{item.label}</div>
                          <div className="text-xs text-muted-foreground">{item.hint}</div>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}

            {/* FAQ list */}
            {view === "faq" && (
              <div>
                <div className="flex items-center gap-2 px-5 py-3 border-b border-primary/8">
                  <button onClick={() => setView("home")} className="text-xs text-primary font-semibold hover:underline">← Back</button>
                  <span className="text-xs text-muted-foreground">/ FAQs</span>
                </div>
                <div className="p-4 space-y-2">
                  {FAQ.map((item, i) => (
                    <button
                      key={i}
                      onClick={() => { setActiveFaq(i); setView("faq-item"); }}
                      className="w-full flex items-start justify-between gap-3 px-4 py-3 rounded-2xl border border-primary/8 bg-white hover:border-primary hover:bg-primary/5 transition-all text-left group"
                    >
                      <span className="text-sm font-medium text-foreground">{item.q}</span>
                      <ChevronRight className="w-4 h-4 text-muted-foreground flex-shrink-0 mt-0.5 group-hover:text-primary transition-colors" />
                    </button>
                  ))}
                </div>
              </div>
            )}

            {/* FAQ item */}
            {view === "faq-item" && activeFaq !== null && (
              <div>
                <div className="flex items-center gap-2 px-5 py-3 border-b border-primary/8">
                  <button onClick={() => setView("faq")} className="text-xs text-primary font-semibold hover:underline">← FAQs</button>
                </div>
                <div className="p-5">
                  <h3 className="text-sm font-bold text-foreground mb-3">{FAQ[activeFaq].q}</h3>
                  <p className="text-sm text-muted-foreground leading-relaxed">{FAQ[activeFaq].a}</p>
                  <div className="mt-5 pt-4 border-t border-primary/8">
                    <p className="text-xs text-muted-foreground mb-2">Still need help?</p>
                    <button
                      onClick={() => setView("contact")}
                      className="text-xs text-primary font-semibold hover:underline flex items-center gap-1"
                    >
                      Send us a message <ExternalLink className="w-3 h-3" />
                    </button>
                  </div>
                </div>
              </div>
            )}

            {/* Contact form */}
            {view === "contact" && (
              <div>
                <div className="flex items-center gap-2 px-5 py-3 border-b border-primary/8">
                  <button onClick={() => setView("home")} className="text-xs text-primary font-semibold hover:underline">← Back</button>
                  <span className="text-xs text-muted-foreground">/ Contact</span>
                </div>
                <form onSubmit={handleSend} className="p-5 space-y-4">
                  <p className="text-xs text-muted-foreground">
                    We'll reply to <span className="font-semibold text-foreground">{user?.emailAddresses[0]?.emailAddress}</span>
                  </p>
                  <div>
                    <label className="text-xs font-semibold text-foreground block mb-1.5 uppercase tracking-wider">Subject</label>
                    <input
                      type="text"
                      value={subject}
                      onChange={(e) => setSubject(e.target.value)}
                      placeholder="e.g. Question about my proxies"
                      className="w-full px-3 py-2.5 rounded-xl border border-primary/15 bg-white text-sm text-foreground focus:outline-none focus:border-primary focus:ring-1 focus:ring-primary/20"
                    />
                  </div>
                  <div>
                    <label className="text-xs font-semibold text-foreground block mb-1.5 uppercase tracking-wider">Message</label>
                    <textarea
                      value={message}
                      onChange={(e) => setMessage(e.target.value)}
                      required
                      rows={4}
                      placeholder="Describe your issue or question..."
                      className="w-full px-3 py-2.5 rounded-xl border border-primary/15 bg-white text-sm text-foreground resize-none focus:outline-none focus:border-primary focus:ring-1 focus:ring-primary/20"
                    />
                  </div>
                  {sendError && (
                    <p className="text-xs text-red-600 bg-red-50 border border-red-200 rounded-xl px-3 py-2">{sendError}</p>
                  )}
                  <button
                    type="submit"
                    disabled={sending}
                    className="w-full py-2.5 rounded-xl text-sm font-bold gold-button text-white flex items-center justify-center gap-2 disabled:opacity-60"
                  >
                    {sending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
                    {sending ? "Sending..." : "Send Message"}
                  </button>
                </form>
              </div>
            )}

            {/* Sent confirmation */}
            {view === "sent" && (
              <div className="p-8 flex flex-col items-center justify-center text-center">
                <div className="w-14 h-14 rounded-2xl bg-green-50 border border-green-200 flex items-center justify-center mb-4">
                  <CheckCircle2 className="w-7 h-7 text-green-600" />
                </div>
                <h3 className="font-bold font-serif text-foreground mb-2">Message sent!</h3>
                <p className="text-sm text-muted-foreground mb-5">
                  We'll get back to you at <span className="font-semibold text-foreground">{user?.emailAddresses[0]?.emailAddress}</span> within a few hours.
                </p>
                <button
                  onClick={reset}
                  className="text-sm text-primary font-semibold hover:underline"
                >
                  Back to support home
                </button>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Floating trigger button */}
      <button
        onClick={() => { setOpen(!open); if (!open) setView("home"); }}
        className="fixed bottom-6 right-6 z-50 w-14 h-14 rounded-2xl gold-button shadow-lg shadow-primary/30 flex items-center justify-center transition-all hover:scale-105 active:scale-95"
        aria-label="Support"
      >
        {open
          ? <X className="w-6 h-6 text-white" />
          : <MessageCircle className="w-6 h-6 text-white" />}
      </button>
    </>
  );
}
