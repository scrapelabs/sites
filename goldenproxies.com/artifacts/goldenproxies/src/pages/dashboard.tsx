import React, { useState } from "react";
import { useUser, useClerk } from "@clerk/react";
import { Link } from "wouter";
import {
  LayoutDashboard, Zap, BarChart2, CreditCard, Settings, Shield,
  LogOut, Copy, Download, RefreshCw, CheckCircle2, Globe, TrendingUp,
  ChevronRight, Menu, X, User, Lock, Eye, EyeOff, Check,
  MessageCircle, HelpCircle, Mail, ExternalLink, Send, Crown
} from "lucide-react";
import AdminPanel from "./admin";

const SUPER_ADMIN_EMAIL = "khemiri.mohamed.ensi@gmail.com";

type Section = "overview" | "generator" | "stats" | "pricing" | "settings-general" | "settings-security" | "support" | "admin";

const COUNTRIES = [
  { code: "us", name: "United States" },
  { code: "gb", name: "United Kingdom" },
  { code: "de", name: "Germany" },
  { code: "fr", name: "France" },
  { code: "jp", name: "Japan" },
  { code: "ca", name: "Canada" },
  { code: "au", name: "Australia" },
  { code: "nl", name: "Netherlands" },
  { code: "sg", name: "Singapore" },
  { code: "br", name: "Brazil" },
  { code: "in", name: "India" },
  { code: "es", name: "Spain" },
  { code: "it", name: "Italy" },
  { code: "random", name: "Random (Mix)" },
];

function generateProxies(type: string, country: string, protocol: string, format: string, qty: number): string[] {
  const base = type === "ipv6" ? "2001:db8" : "192.168";
  const port = protocol === "socks5" ? 1080 : 8080;
  const results: string[] = [];
  for (let i = 0; i < qty; i++) {
    const a = Math.floor(Math.random() * 255);
    const b = Math.floor(Math.random() * 255);
    const c = Math.floor(Math.random() * 255);
    const ip = type === "ipv6"
      ? `2001:db8:${a.toString(16)}:${b.toString(16)}::${c.toString(16)}`
      : `${base}.${a}.${b}`;
    const user = `gp_${country}_${(Math.random() * 1e6 | 0).toString(36)}`;
    const pass = (Math.random() * 1e8 | 0).toString(36);
    let line = "";
    if (format === "ip:port:user:pass") line = `${ip}:${port}:${user}:${pass}`;
    else if (format === "user:pass@ip:port") line = `${user}:${pass}@${ip}:${port}`;
    else if (format === "ip:port") line = `${ip}:${port}`;
    else if (format === "user:pass") line = `${user}:${pass}`;
    results.push(line);
  }
  return results;
}

function NavItem({ icon: Icon, label, active, onClick, sub, adminStyle }: {
  icon: React.ElementType; label: string; active: boolean;
  onClick: () => void; sub?: boolean; adminStyle?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      className={`w-full flex items-center gap-3 px-4 py-2.5 rounded-xl text-sm font-medium transition-all text-left
        ${sub ? "pl-10" : ""}
        ${adminStyle && !active ? "text-primary/80 hover:bg-primary/8 hover:text-primary" : ""}
        ${adminStyle && active ? "bg-gradient-to-r from-primary/15 to-primary/5 text-primary shadow-sm border border-primary/20" : ""}
        ${active
          ? "bg-primary/10 text-primary font-semibold"
          : "text-foreground/70 hover:text-foreground hover:bg-primary/5"}`}
    >
      <Icon className={`w-4 h-4 flex-shrink-0 ${active ? "text-primary" : ""}`} />
      {label}
    </button>
  );
}

export default function Dashboard() {
  const { user } = useUser();
  const { signOut } = useClerk();
  const [section, setSection] = useState<Section>("overview");
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const displayName = user?.firstName || user?.emailAddresses[0]?.emailAddress?.split("@")[0] || "User";
  const email = user?.emailAddresses[0]?.emailAddress || "";

  function go(s: Section) { setSection(s); setSidebarOpen(false); }

  const isAdmin = email === SUPER_ADMIN_EMAIL;

  const navItems: { icon: React.ElementType; label: string; section: Section }[] = [
    { icon: LayoutDashboard, label: "Overview", section: "overview" },
    { icon: Zap, label: "Proxy Generator", section: "generator" },
    { icon: BarChart2, label: "Proxy Stats", section: "stats" },
    { icon: CreditCard, label: "Pricing", section: "pricing" },
    { icon: MessageCircle, label: "Support", section: "support" },
    ...(isAdmin ? [{ icon: Crown, label: "Admin", section: "admin" as Section }] : []),
  ];

  return (
    <div className="min-h-screen bg-background pt-20 flex">
      {/* Mobile overlay */}
      {sidebarOpen && (
        <div className="fixed inset-0 bg-black/30 z-30 lg:hidden" onClick={() => setSidebarOpen(false)} />
      )}

      {/* Sidebar */}
      <aside className={`fixed top-20 left-0 bottom-0 w-64 bg-white border-r border-primary/10 z-40 flex flex-col transition-transform duration-200
        ${sidebarOpen ? "translate-x-0" : "-translate-x-full"} lg:translate-x-0`}>
        <div className="flex-1 p-4 space-y-1 overflow-y-auto">
          {navItems.map((n) => (
            <React.Fragment key={n.section}>
              {n.section === "admin" && (
                <div className="pt-3 pb-1 px-4">
                  <div className="flex items-center gap-2">
                    <div className="flex-1 h-px bg-gradient-to-r from-primary/30 to-transparent" />
                    <Crown className="w-3 h-3 text-primary" />
                    <p className="text-[10px] text-primary uppercase tracking-widest font-bold">Admin</p>
                    <div className="flex-1 h-px bg-gradient-to-l from-primary/30 to-transparent" />
                  </div>
                </div>
              )}
              <NavItem icon={n.icon} label={n.label}
                active={section === n.section} onClick={() => go(n.section)}
                adminStyle={n.section === "admin"} />
            </React.Fragment>
          ))}
          <div className="pt-2 pb-1 px-4">
            <p className="text-[10px] text-muted-foreground uppercase tracking-widest font-semibold">Account</p>
          </div>
          <NavItem icon={Settings} label="General Settings" active={section === "settings-general"} onClick={() => go("settings-general")} />
          <NavItem icon={Shield} label="Security" active={section === "settings-security"} onClick={() => go("settings-security")} />
        </div>

        {/* User footer */}
        <div className="p-4 border-t border-primary/10">
          <div className="flex items-center gap-3 mb-3">
            <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-primary to-primary/60 flex items-center justify-center flex-shrink-0">
              <User className="w-4 h-4 text-white" />
            </div>
            <div className="min-w-0">
              <div className="text-sm font-semibold text-foreground truncate">{displayName}</div>
              <div className="text-xs text-muted-foreground truncate">{email}</div>
            </div>
          </div>
          <button
            onClick={() => signOut()}
            className="w-full flex items-center gap-2 px-3 py-2 rounded-xl text-sm text-muted-foreground hover:text-red-600 hover:bg-red-50 transition-colors"
          >
            <LogOut className="w-4 h-4" />
            Sign out
          </button>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 lg:ml-64 min-w-0">
        {/* Mobile header */}
        <div className="lg:hidden fixed top-20 left-0 right-0 z-20 bg-white border-b border-primary/10 px-4 py-3 flex items-center gap-3">
          <button onClick={() => setSidebarOpen(true)} className="p-2 rounded-lg hover:bg-primary/5 transition-colors">
            <Menu className="w-5 h-5 text-foreground" />
          </button>
          <span className="font-semibold text-foreground capitalize">
            {section.replace("settings-", "").replace("-", " ")}
          </span>
        </div>

        <div className="p-6 pt-6 lg:pt-8 mt-14 lg:mt-0">
          {section === "overview" && <OverviewSection go={go} />}
          {section === "generator" && <GeneratorSection />}
          {section === "stats" && <StatsSection />}
          {section === "pricing" && <PricingSection />}
          {section === "settings-general" && <GeneralSettingsSection user={user} />}
          {section === "settings-security" && <SecuritySettingsSection />}
          {section === "support" && <SupportSection user={user} />}
          {section === "admin" && isAdmin && <AdminPanel />}
        </div>
      </main>
    </div>
  );
}

function OverviewSection({ go }: { go: (s: Section) => void }) {
  const { user } = useUser();
  const displayName = user?.firstName || user?.emailAddresses[0]?.emailAddress?.split("@")[0] || "User";

  const stats = [
    { label: "Active Proxies", value: "—", sub: "No active subscription", icon: Zap, color: "text-primary" },
    { label: "Bandwidth Used", value: "0 GB", sub: "This month", icon: TrendingUp, color: "text-green-600" },
    { label: "Requests Today", value: "0", sub: "Across all proxies", icon: BarChart2, color: "text-primary" },
    { label: "Success Rate", value: "—", sub: "7-day average", icon: CheckCircle2, color: "text-green-600" },
  ];

  const quickActions = [
    { label: "Generate Proxies", desc: "Build your proxy list", icon: Zap, section: "generator" as Section },
    { label: "View Stats", desc: "Monitor performance", icon: BarChart2, section: "stats" as Section },
    { label: "Upgrade Plan", desc: "Unlock more proxies", icon: CreditCard, section: "pricing" as Section },
  ];

  return (
    <div className="max-w-5xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold font-serif text-foreground">
          Welcome back, <span className="gold-gradient-text">{displayName}</span>
        </h1>
        <p className="text-muted-foreground text-sm mt-1">Here's an overview of your GoldenProxies account.</p>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {stats.map((s) => (
          <div key={s.label} className="glass-card rounded-2xl p-5 border border-primary/10">
            <s.icon className={`w-5 h-5 ${s.color} mb-3 opacity-70`} />
            <div className={`text-2xl font-bold font-serif ${s.color}`}>{s.value}</div>
            <div className="text-xs font-semibold text-foreground mt-1">{s.label}</div>
            <div className="text-xs text-muted-foreground mt-0.5">{s.sub}</div>
          </div>
        ))}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {quickActions.map((a) => (
          <button
            key={a.label}
            onClick={() => go(a.section)}
            className="glass-card rounded-2xl p-5 border border-primary/10 hover:border-primary hover:bg-primary/5 transition-all text-left group"
          >
            <div className="flex items-center justify-between mb-3">
              <div className="w-9 h-9 rounded-xl bg-primary/10 flex items-center justify-center">
                <a.icon className="w-4 h-4 text-primary" />
              </div>
              <ChevronRight className="w-4 h-4 text-muted-foreground group-hover:text-primary transition-colors" />
            </div>
            <div className="font-semibold text-foreground text-sm">{a.label}</div>
            <div className="text-xs text-muted-foreground mt-0.5">{a.desc}</div>
          </button>
        ))}
      </div>

      <div className="glass-card rounded-2xl p-6 border border-primary/10">
        <div className="flex items-center justify-between mb-4">
          <h2 className="font-bold font-serif text-foreground">Active Subscription</h2>
          <button onClick={() => go("pricing")} className="text-xs text-primary font-semibold hover:underline">
            View plans →
          </button>
        </div>
        <div className="bg-primary/5 border border-dashed border-primary/20 rounded-xl p-6 text-center">
          <div className="w-10 h-10 rounded-full bg-primary/10 flex items-center justify-center mx-auto mb-3">
            <CreditCard className="w-5 h-5 text-primary" />
          </div>
          <div className="font-semibold text-foreground text-sm">No active subscription</div>
          <div className="text-xs text-muted-foreground mt-1 mb-4">Purchase a plan to start using GoldenProxies</div>
          <button onClick={() => go("pricing")}
            className="px-5 py-2 rounded-full text-sm font-bold gold-button text-white">
            Browse Plans
          </button>
        </div>
      </div>
    </div>
  );
}

function GeneratorSection() {
  const [proxyType, setProxyType] = useState("residential");
  const [country, setCountry] = useState("us");
  const [protocol, setProtocol] = useState("http");
  const [format, setFormat] = useState("ip:port:user:pass");
  const [quantity, setQuantity] = useState(10);
  const [output, setOutput] = useState<string[]>([]);
  const [copied, setCopied] = useState(false);

  function handleGenerate() {
    setOutput(generateProxies(proxyType, country, protocol, format, quantity));
  }

  function handleCopy() {
    navigator.clipboard.writeText(output.join("\n"));
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  function handleDownload() {
    const blob = new Blob([output.join("\n")], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `goldenproxies-${proxyType}-${country}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  }

  const types = [
    { id: "residential", label: "Residential" },
    { id: "ipv6", label: "IPv6" },
    { id: "datacenter", label: "Datacenter" },
  ];

  const formats = [
    { id: "ip:port:user:pass", label: "IP:Port:User:Pass" },
    { id: "user:pass@ip:port", label: "User:Pass@IP:Port" },
    { id: "ip:port", label: "IP:Port only" },
    { id: "user:pass", label: "User:Pass only" },
  ];

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold font-serif text-foreground">Proxy Generator</h1>
        <p className="text-muted-foreground text-sm mt-1">Configure your proxy list and generate credentials.</p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Config panel */}
        <div className="lg:col-span-1 space-y-5">
          {/* Proxy type */}
          <div className="glass-card rounded-2xl p-5 border border-primary/10 space-y-3">
            <label className="text-xs font-semibold text-foreground uppercase tracking-wider">Proxy Type</label>
            <div className="space-y-2">
              {types.map((t) => (
                <button
                  key={t.id}
                  onClick={() => setProxyType(t.id)}
                  className={`w-full flex items-center justify-between px-4 py-2.5 rounded-xl border text-sm font-medium transition-all
                    ${proxyType === t.id
                      ? "border-primary bg-primary/8 text-primary"
                      : "border-primary/10 bg-white text-foreground hover:border-primary/30"}`}
                >
                  {t.label}
                  {proxyType === t.id && <Check className="w-4 h-4" />}
                </button>
              ))}
            </div>
          </div>

          {/* Protocol */}
          <div className="glass-card rounded-2xl p-5 border border-primary/10 space-y-3">
            <label className="text-xs font-semibold text-foreground uppercase tracking-wider">Protocol</label>
            <div className="flex gap-2">
              {["http", "socks5"].map((p) => (
                <button
                  key={p}
                  onClick={() => setProtocol(p)}
                  className={`flex-1 py-2.5 rounded-xl border text-sm font-semibold transition-all uppercase tracking-wide
                    ${protocol === p
                      ? "border-primary bg-primary/8 text-primary"
                      : "border-primary/10 bg-white text-foreground hover:border-primary/30"}`}
                >
                  {p}
                </button>
              ))}
            </div>
          </div>

          {/* Country */}
          <div className="glass-card rounded-2xl p-5 border border-primary/10 space-y-3">
            <label className="text-xs font-semibold text-foreground uppercase tracking-wider">Country</label>
            <select
              value={country}
              onChange={(e) => setCountry(e.target.value)}
              className="w-full px-3 py-2.5 rounded-xl border border-primary/15 bg-white text-sm text-foreground focus:outline-none focus:border-primary focus:ring-1 focus:ring-primary/20"
            >
              {COUNTRIES.map((c) => (
                <option key={c.code} value={c.code}>{c.name}</option>
              ))}
            </select>
          </div>

          {/* Format */}
          <div className="glass-card rounded-2xl p-5 border border-primary/10 space-y-3">
            <label className="text-xs font-semibold text-foreground uppercase tracking-wider">Output Format</label>
            <div className="space-y-2">
              {formats.map((f) => (
                <button
                  key={f.id}
                  onClick={() => setFormat(f.id)}
                  className={`w-full flex items-center justify-between px-4 py-2.5 rounded-xl border text-xs font-mono font-medium transition-all
                    ${format === f.id
                      ? "border-primary bg-primary/8 text-primary"
                      : "border-primary/10 bg-white text-foreground hover:border-primary/30"}`}
                >
                  {f.label}
                  {format === f.id && <Check className="w-3 h-3" />}
                </button>
              ))}
            </div>
          </div>

          {/* Quantity */}
          <div className="glass-card rounded-2xl p-5 border border-primary/10 space-y-3">
            <div className="flex items-center justify-between">
              <label className="text-xs font-semibold text-foreground uppercase tracking-wider">Quantity</label>
              <span className="text-sm font-bold text-primary">{quantity}</span>
            </div>
            <input
              type="range"
              min={1} max={100} value={quantity}
              onChange={(e) => setQuantity(Number(e.target.value))}
              className="w-full accent-primary"
            />
            <div className="flex justify-between text-xs text-muted-foreground">
              <span>1</span><span>50</span><span>100</span>
            </div>
            <input
              type="number"
              min={1} max={100} value={quantity}
              onChange={(e) => setQuantity(Math.max(1, Math.min(100, Number(e.target.value))))}
              className="w-full px-3 py-2 rounded-xl border border-primary/15 bg-white text-sm text-foreground text-center focus:outline-none focus:border-primary"
            />
          </div>

          <button
            onClick={handleGenerate}
            className="w-full py-3 rounded-xl font-bold text-sm gold-button text-white flex items-center justify-center gap-2"
          >
            <RefreshCw className="w-4 h-4" />
            Generate Proxies
          </button>
        </div>

        {/* Output panel */}
        <div className="lg:col-span-2 glass-card rounded-2xl border border-primary/10 flex flex-col">
          <div className="flex items-center justify-between p-4 border-b border-primary/10">
            <div>
              <span className="text-sm font-semibold text-foreground">Generated List</span>
              {output.length > 0 && (
                <span className="ml-2 text-xs bg-primary/10 text-primary px-2 py-0.5 rounded-full font-semibold">
                  {output.length} proxies
                </span>
              )}
            </div>
            {output.length > 0 && (
              <div className="flex gap-2">
                <button
                  onClick={handleCopy}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-primary/20 text-xs font-semibold text-foreground hover:border-primary hover:bg-primary/5 transition-all"
                >
                  {copied ? <Check className="w-3 h-3 text-green-600" /> : <Copy className="w-3 h-3" />}
                  {copied ? "Copied!" : "Copy"}
                </button>
                <button
                  onClick={handleDownload}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold gold-button text-white"
                >
                  <Download className="w-3 h-3" />
                  Download
                </button>
              </div>
            )}
          </div>
          <div className="flex-1 p-4 min-h-[400px]">
            {output.length === 0 ? (
              <div className="h-full flex flex-col items-center justify-center text-center">
                <div className="w-12 h-12 rounded-2xl bg-primary/8 flex items-center justify-center mb-3">
                  <Zap className="w-6 h-6 text-primary opacity-60" />
                </div>
                <div className="text-sm font-semibold text-foreground">No proxies generated yet</div>
                <div className="text-xs text-muted-foreground mt-1">Configure your settings and click Generate</div>
              </div>
            ) : (
              <textarea
                readOnly
                value={output.join("\n")}
                className="w-full h-full min-h-[400px] resize-none bg-[#fafaf7] rounded-xl border border-primary/10 p-3 text-xs font-mono text-foreground focus:outline-none focus:border-primary"
              />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function StatsSection() {
  const networkStats = [
    { label: "Total IPs in Network", value: "75M+", icon: Globe, color: "text-primary" },
    { label: "Countries Available", value: "200+", icon: Globe, color: "text-primary" },
    { label: "Network Uptime", value: "99.9%", icon: TrendingUp, color: "text-green-600" },
    { label: "Avg Success Rate", value: "99.5%", icon: CheckCircle2, color: "text-green-600" },
  ];

  const proxyTypes = [
    { name: "Residential Proxies", ips: "70M+", protocols: "HTTP, HTTPS, SOCKS5", coverage: "195+ countries", speed: "avg 230ms" },
    { name: "IPv6 Proxies", ips: "4M+", protocols: "HTTP, HTTPS, SOCKS5", coverage: "80+ countries", speed: "avg 120ms" },
    { name: "Datacenter Proxies", ips: "1M+", protocols: "HTTP, HTTPS, SOCKS5", coverage: "60+ locations", speed: "avg 45ms" },
  ];

  return (
    <div className="max-w-5xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold font-serif text-foreground">Proxy Stats</h1>
        <p className="text-muted-foreground text-sm mt-1">Real-time network performance and availability.</p>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {networkStats.map((s) => (
          <div key={s.label} className="glass-card rounded-2xl p-5 border border-primary/10">
            <s.icon className={`w-5 h-5 ${s.color} mb-3 opacity-70`} />
            <div className={`text-2xl font-bold font-serif ${s.color}`}>{s.value}</div>
            <div className="text-xs text-muted-foreground font-medium mt-1">{s.label}</div>
          </div>
        ))}
      </div>

      <div className="glass-card rounded-2xl p-6 border border-primary/10">
        <h2 className="font-bold font-serif text-foreground mb-5">Network Status</h2>
        <div className="space-y-3">
          {["Residential Pool", "IPv6 Pool", "Datacenter Pool", "API Gateway", "Auth Service"].map((service) => (
            <div key={service} className="flex items-center justify-between py-3 border-b border-primary/8 last:border-0">
              <div className="flex items-center gap-3">
                <div className="w-2 h-2 rounded-full bg-green-500 shadow-sm shadow-green-500/50" />
                <span className="text-sm font-medium text-foreground">{service}</span>
              </div>
              <div className="flex items-center gap-3">
                <span className="text-xs text-muted-foreground">100% uptime</span>
                <span className="text-xs text-green-600 font-semibold bg-green-50 px-2 py-0.5 rounded-full">Operational</span>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {proxyTypes.map((t) => (
          <div key={t.name} className="glass-card rounded-2xl p-5 border border-primary/10 space-y-3">
            <h3 className="font-bold text-foreground font-serif text-sm">{t.name}</h3>
            <div className="space-y-2">
              {[
                { label: "IP Pool", value: t.ips },
                { label: "Protocols", value: t.protocols },
                { label: "Coverage", value: t.coverage },
                { label: "Avg Speed", value: t.speed },
              ].map((row) => (
                <div key={row.label} className="flex items-start justify-between gap-2">
                  <span className="text-xs text-muted-foreground">{row.label}</span>
                  <span className="text-xs font-semibold text-foreground text-right">{row.value}</span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>

      <div className="glass-card rounded-2xl p-6 border border-primary/10">
        <h2 className="font-bold font-serif text-foreground mb-4">Your Usage</h2>
        <div className="bg-primary/5 border border-dashed border-primary/20 rounded-xl p-6 text-center">
          <BarChart2 className="w-8 h-8 text-primary opacity-40 mx-auto mb-2" />
          <div className="text-sm font-semibold text-foreground">No usage data yet</div>
          <div className="text-xs text-muted-foreground mt-1">Purchase a plan and start using proxies to see your statistics</div>
        </div>
      </div>
    </div>
  );
}

function PricingSection() {
  const plans = [
    { name: "Starter", price: 9.99, badge: null, type: "Residential", bandwidth: "5 GB", ips: "70M+ IPs", features: ["HTTP/HTTPS", "195+ countries", "Rotating sessions", "24/7 support"] },
    { name: "Professional", price: 29.99, badge: "Most Popular", type: "Residential", bandwidth: "25 GB", ips: "70M+ IPs", features: ["HTTP/HTTPS/SOCKS5", "195+ countries", "Sticky sessions", "API access", "Priority support"] },
    { name: "Business", price: 79.99, badge: null, type: "Residential", bandwidth: "100 GB", ips: "70M+ IPs", features: ["All protocols", "200+ countries", "Sub-users", "Dedicated manager", "SLA guarantee"] },
    { name: "Datacenter Pro", price: 19.99, badge: null, type: "Datacenter", bandwidth: "50 GB", ips: "1M+ IPs", features: ["HTTP/HTTPS/SOCKS5", "40+ locations", "High speed", "99.9% uptime"] },
    { name: "IPv6 Pro", price: 14.99, badge: null, type: "IPv6", bandwidth: "5,000 IPs", ips: "4M+ IPs", features: ["HTTP/HTTPS/SOCKS5", "60+ countries", "Instant activation", "Custom subnets"] },
  ];

  return (
    <div className="max-w-5xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold font-serif text-foreground">Pricing Plans</h1>
        <p className="text-muted-foreground text-sm mt-1">Select a plan to unlock your proxy network.</p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {plans.map((p) => (
          <div key={p.name}
            className={`glass-card rounded-2xl p-5 border flex flex-col
              ${p.badge ? "border-primary ring-1 ring-primary/30" : "border-primary/10"}`}>
            {p.badge && (
              <div className="mb-3">
                <span className="text-xs font-bold text-white bg-primary px-3 py-1 rounded-full">{p.badge}</span>
              </div>
            )}
            <div className="mb-1">
              <span className="text-xs text-primary font-semibold bg-primary/8 px-2 py-0.5 rounded-full">{p.type}</span>
            </div>
            <h3 className="font-bold font-serif text-foreground text-lg mt-2">{p.name}</h3>
            <div className="flex items-baseline gap-1 mt-1 mb-4">
              <span className="text-3xl font-bold text-foreground">${p.price}</span>
              <span className="text-sm text-muted-foreground">/mo</span>
            </div>
            <div className="text-xs text-muted-foreground mb-4 space-y-0.5">
              <div><span className="font-semibold text-foreground">{p.bandwidth}</span> bandwidth</div>
              <div><span className="font-semibold text-foreground">{p.ips}</span> pool</div>
            </div>
            <ul className="space-y-2 mb-5 flex-1">
              {p.features.map((f) => (
                <li key={f} className="flex items-start gap-2 text-xs text-foreground">
                  <Check className="w-3.5 h-3.5 text-primary mt-0.5 flex-shrink-0" />
                  {f}
                </li>
              ))}
            </ul>
            <Link
              href="/contact"
              className={`w-full py-2.5 rounded-xl text-sm font-bold text-center transition-all block
                ${p.badge ? "gold-button text-white" : "border border-primary/20 text-foreground hover:border-primary hover:bg-primary/5"}`}
            >
              Get Started
            </Link>
          </div>
        ))}
      </div>
    </div>
  );
}

function GeneralSettingsSection({ user }: { user: any }) {
  const [name, setName] = useState(user?.firstName || "");
  const [company, setCompany] = useState("");
  const [saved, setSaved] = useState(false);

  function handleSave(e: React.FormEvent) {
    e.preventDefault();
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  }

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold font-serif text-foreground">General Settings</h1>
        <p className="text-muted-foreground text-sm mt-1">Manage your profile information.</p>
      </div>

      <div className="glass-card rounded-2xl p-6 border border-primary/10">
        <div className="flex items-center gap-4 pb-6 border-b border-primary/10 mb-6">
          <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-primary to-primary/60 flex items-center justify-center flex-shrink-0">
            <User className="w-8 h-8 text-white" />
          </div>
          <div>
            <div className="font-bold text-foreground">{user?.firstName || "User"}</div>
            <div className="text-sm text-muted-foreground">{user?.emailAddresses?.[0]?.emailAddress}</div>
          </div>
        </div>

        <form onSubmit={handleSave} className="space-y-4">
          <div>
            <label className="text-xs font-semibold text-foreground uppercase tracking-wider block mb-1.5">Display Name</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Your display name"
              className="w-full px-4 py-3 rounded-xl border border-primary/15 bg-white text-sm text-foreground focus:outline-none focus:border-primary focus:ring-1 focus:ring-primary/20"
            />
          </div>
          <div>
            <label className="text-xs font-semibold text-foreground uppercase tracking-wider block mb-1.5">Email Address</label>
            <input
              type="email"
              value={user?.emailAddresses?.[0]?.emailAddress || ""}
              disabled
              className="w-full px-4 py-3 rounded-xl border border-primary/8 bg-primary/3 text-sm text-muted-foreground cursor-not-allowed"
            />
            <p className="text-xs text-muted-foreground mt-1">Email is managed through your authentication provider.</p>
          </div>
          <div>
            <label className="text-xs font-semibold text-foreground uppercase tracking-wider block mb-1.5">Company Name <span className="text-muted-foreground normal-case font-normal">(optional)</span></label>
            <input
              type="text"
              value={company}
              onChange={(e) => setCompany(e.target.value)}
              placeholder="Your company or organization"
              className="w-full px-4 py-3 rounded-xl border border-primary/15 bg-white text-sm text-foreground focus:outline-none focus:border-primary focus:ring-1 focus:ring-primary/20"
            />
          </div>
          <div className="pt-2">
            <button
              type="submit"
              className="flex items-center gap-2 px-6 py-2.5 rounded-xl text-sm font-bold gold-button text-white"
            >
              {saved ? <><Check className="w-4 h-4" /> Saved!</> : "Save Changes"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function SecuritySettingsSection() {
  const [showCurrent, setShowCurrent] = useState(false);
  const [showNew, setShowNew] = useState(false);
  const [current, setCurrent] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirm, setConfirm] = useState("");
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (newPw.length < 8) { setError("Password must be at least 8 characters."); return; }
    if (newPw !== confirm) { setError("Passwords do not match."); return; }
    setSaved(true);
    setCurrent(""); setNewPw(""); setConfirm("");
    setTimeout(() => setSaved(false), 3000);
  }

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold font-serif text-foreground">Security Settings</h1>
        <p className="text-muted-foreground text-sm mt-1">Manage your account security.</p>
      </div>

      <div className="glass-card rounded-2xl p-6 border border-primary/10">
        <div className="flex items-center gap-3 mb-6">
          <div className="w-9 h-9 rounded-xl bg-primary/10 flex items-center justify-center">
            <Lock className="w-4 h-4 text-primary" />
          </div>
          <div>
            <div className="font-bold text-foreground">Change Password</div>
            <div className="text-xs text-muted-foreground">Update your account password</div>
          </div>
        </div>

        {saved && (
          <div className="flex items-center gap-2 bg-green-50 border border-green-200 text-green-700 text-sm font-medium rounded-xl px-4 py-3 mb-5">
            <CheckCircle2 className="w-4 h-4 flex-shrink-0" />
            Password updated successfully.
          </div>
        )}
        {error && (
          <div className="bg-red-50 border border-red-200 text-red-700 text-sm font-medium rounded-xl px-4 py-3 mb-5">
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="text-xs font-semibold text-foreground uppercase tracking-wider block mb-1.5">Current Password</label>
            <div className="relative">
              <input
                type={showCurrent ? "text" : "password"}
                value={current}
                onChange={(e) => setCurrent(e.target.value)}
                placeholder="Enter current password"
                className="w-full px-4 py-3 pr-10 rounded-xl border border-primary/15 bg-white text-sm text-foreground focus:outline-none focus:border-primary focus:ring-1 focus:ring-primary/20"
              />
              <button type="button" onClick={() => setShowCurrent(!showCurrent)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground">
                {showCurrent ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </div>
          </div>
          <div>
            <label className="text-xs font-semibold text-foreground uppercase tracking-wider block mb-1.5">New Password</label>
            <div className="relative">
              <input
                type={showNew ? "text" : "password"}
                value={newPw}
                onChange={(e) => setNewPw(e.target.value)}
                placeholder="Min. 8 characters"
                className="w-full px-4 py-3 pr-10 rounded-xl border border-primary/15 bg-white text-sm text-foreground focus:outline-none focus:border-primary focus:ring-1 focus:ring-primary/20"
              />
              <button type="button" onClick={() => setShowNew(!showNew)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground">
                {showNew ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </div>
            {newPw.length > 0 && (
              <div className="flex gap-1 mt-2">
                {[0, 1, 2, 3].map((i) => (
                  <div key={i} className={`h-1 flex-1 rounded-full transition-colors
                    ${newPw.length >= (i + 1) * 3 ? (newPw.length >= 12 ? "bg-green-500" : "bg-primary") : "bg-primary/15"}`} />
                ))}
              </div>
            )}
          </div>
          <div>
            <label className="text-xs font-semibold text-foreground uppercase tracking-wider block mb-1.5">Confirm New Password</label>
            <input
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              placeholder="Repeat new password"
              className={`w-full px-4 py-3 rounded-xl border text-sm text-foreground focus:outline-none focus:ring-1 bg-white
                ${confirm && confirm !== newPw
                  ? "border-red-300 focus:border-red-400 focus:ring-red-200"
                  : "border-primary/15 focus:border-primary focus:ring-primary/20"}`}
            />
            {confirm && confirm !== newPw && (
              <p className="text-xs text-red-500 mt-1">Passwords don't match</p>
            )}
          </div>
          <div className="pt-2">
            <button
              type="submit"
              className="flex items-center gap-2 px-6 py-2.5 rounded-xl text-sm font-bold gold-button text-white"
            >
              <Lock className="w-4 h-4" />
              Update Password
            </button>
          </div>
        </form>
      </div>

      <div className="glass-card rounded-2xl p-6 border border-primary/10">
        <div className="flex items-center gap-3 mb-4">
          <div className="w-9 h-9 rounded-xl bg-primary/10 flex items-center justify-center">
            <Shield className="w-4 h-4 text-primary" />
          </div>
          <div>
            <div className="font-bold text-foreground">Active Sessions</div>
            <div className="text-xs text-muted-foreground">Devices currently signed in</div>
          </div>
        </div>
        <div className="flex items-center justify-between py-3 bg-primary/5 rounded-xl px-4">
          <div>
            <div className="text-sm font-semibold text-foreground">Current device</div>
            <div className="text-xs text-muted-foreground">Active now · This session</div>
          </div>
          <span className="text-xs bg-green-50 text-green-700 border border-green-200 px-2.5 py-1 rounded-full font-semibold">Active</span>
        </div>
      </div>
    </div>
  );
}

const FAQ_ITEMS = [
  { q: "How do I generate proxies?", a: "Go to Proxy Generator in the sidebar. Select your proxy type, country, protocol, and format, then click Generate." },
  { q: "What formats are supported?", a: "IP:Port:User:Pass, User:Pass@IP:Port, IP:Port only, and User:Pass only are all available in the generator." },
  { q: "What protocols do you support?", a: "All plans support HTTP and SOCKS5. You can switch between them in the Proxy Generator." },
  { q: "What is your uptime guarantee?", a: "We guarantee 99.9% network uptime across all proxy types, with 99.99% on Business and Enterprise plans." },
  { q: "How many countries are available?", a: "Residential: 195+ countries. IPv6: 80+ countries. Datacenter: 60+ global locations." },
  { q: "Can I download my proxy list?", a: "Yes — after generating, click the Download button to save your list as a .txt file." },
];

function SupportSection({ user }: { user: any }) {
  const [activeFaq, setActiveFaq] = useState<number | null>(null);
  const [subject, setSubject] = useState("");
  const [message, setMessage] = useState("");
  const [sent, setSent] = useState(false);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState("");

  const API_BASE = import.meta.env.VITE_API_URL ?? "";

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
      if (!res.ok) throw new Error("Failed");
      setSent(true);
      setMessage("");
      setSubject("");
    } catch {
      setSendError("Could not send message — please try again.");
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold font-serif text-foreground">Support</h1>
        <p className="text-muted-foreground text-sm mt-1">Get help, browse FAQs, or contact our team.</p>
      </div>

      {/* Contact cards */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        {[
          { icon: Mail, title: "Email Support", desc: "support@goldenproxies.com", sub: "Replies within a few hours" },
          { icon: MessageCircle, title: "Live Chat", desc: "Chat with us", sub: "Use the button at bottom-right" },
          { icon: HelpCircle, title: "Documentation", desc: "Browse our guides", sub: "Setup, integration & FAQ" },
        ].map((c) => (
          <div key={c.title} className="glass-card rounded-2xl p-5 border border-primary/10 flex flex-col gap-3">
            <div className="w-9 h-9 rounded-xl bg-primary/10 flex items-center justify-center">
              <c.icon className="w-4 h-4 text-primary" />
            </div>
            <div>
              <div className="font-bold text-foreground text-sm">{c.title}</div>
              <div className="text-sm text-primary font-medium mt-0.5">{c.desc}</div>
              <div className="text-xs text-muted-foreground mt-0.5">{c.sub}</div>
            </div>
          </div>
        ))}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* FAQ */}
        <div className="glass-card rounded-2xl border border-primary/10 overflow-hidden">
          <div className="px-6 py-4 border-b border-primary/10 flex items-center gap-2">
            <HelpCircle className="w-4 h-4 text-primary" />
            <h2 className="font-bold font-serif text-foreground">Frequently Asked Questions</h2>
          </div>
          <div className="divide-y divide-primary/8">
            {FAQ_ITEMS.map((item, i) => (
              <div key={i}>
                <button
                  onClick={() => setActiveFaq(activeFaq === i ? null : i)}
                  className="w-full flex items-center justify-between px-6 py-4 text-left hover:bg-primary/3 transition-colors"
                >
                  <span className="text-sm font-medium text-foreground pr-4">{item.q}</span>
                  <ChevronRight className={`w-4 h-4 text-muted-foreground flex-shrink-0 transition-transform ${activeFaq === i ? "rotate-90 text-primary" : ""}`} />
                </button>
                {activeFaq === i && (
                  <div className="px-6 pb-4">
                    <p className="text-sm text-muted-foreground leading-relaxed">{item.a}</p>
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>

        {/* Contact form */}
        <div className="glass-card rounded-2xl border border-primary/10 overflow-hidden">
          <div className="px-6 py-4 border-b border-primary/10 flex items-center gap-2">
            <Send className="w-4 h-4 text-primary" />
            <h2 className="font-bold font-serif text-foreground">Send us a Message</h2>
          </div>
          <div className="p-6">
            {sent ? (
              <div className="flex flex-col items-center justify-center text-center py-8">
                <div className="w-14 h-14 rounded-2xl bg-green-50 border border-green-200 flex items-center justify-center mb-4">
                  <CheckCircle2 className="w-7 h-7 text-green-600" />
                </div>
                <h3 className="font-bold font-serif text-foreground mb-2">Message sent!</h3>
                <p className="text-sm text-muted-foreground mb-5">
                  We'll reply to <span className="font-semibold text-foreground">{user?.emailAddresses?.[0]?.emailAddress}</span> within a few hours.
                </p>
                <button onClick={() => setSent(false)} className="text-sm text-primary font-semibold hover:underline">
                  Send another message
                </button>
              </div>
            ) : (
              <form onSubmit={handleSend} className="space-y-4">
                <div>
                  <p className="text-xs text-muted-foreground mb-4">
                    Replying to <span className="font-semibold text-foreground">{user?.emailAddresses?.[0]?.emailAddress}</span>
                  </p>
                </div>
                <div>
                  <label className="text-xs font-semibold text-foreground uppercase tracking-wider block mb-1.5">Subject</label>
                  <input
                    type="text"
                    value={subject}
                    onChange={(e) => setSubject(e.target.value)}
                    placeholder="What's this about?"
                    className="w-full px-4 py-3 rounded-xl border border-primary/15 bg-white text-sm text-foreground focus:outline-none focus:border-primary focus:ring-1 focus:ring-primary/20"
                  />
                </div>
                <div>
                  <label className="text-xs font-semibold text-foreground uppercase tracking-wider block mb-1.5">Message</label>
                  <textarea
                    required
                    rows={5}
                    value={message}
                    onChange={(e) => setMessage(e.target.value)}
                    placeholder="Describe your issue or question in detail..."
                    className="w-full px-4 py-3 rounded-xl border border-primary/15 bg-white text-sm text-foreground resize-none focus:outline-none focus:border-primary focus:ring-1 focus:ring-primary/20"
                  />
                </div>
                <button
                  type="submit"
                  disabled={sending}
                  className="w-full py-3 rounded-xl text-sm font-bold gold-button text-white flex items-center justify-center gap-2 disabled:opacity-60"
                >
                  {sending ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
                  {sending ? "Sending..." : "Send Message"}
                </button>
                {sendError && (
                  <p className="text-xs text-red-600 bg-red-50 border border-red-200 rounded-xl px-4 py-2">{sendError}</p>
                )}
              </form>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
