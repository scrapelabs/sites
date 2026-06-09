import React, { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Users, DollarSign, ShoppingBag, TrendingUp, RefreshCw,
  Search, Crown, Globe, Zap, BarChart2, CheckCircle2,
  XCircle, Clock, Ban, ChevronDown, ChevronUp,
  MessageSquare, Send, Inbox, ChevronRight, Check, Loader2, X
} from "lucide-react";

const API_BASE = import.meta.env.VITE_API_URL ?? "";

async function apiFetch(path: string, opts?: RequestInit) {
  const res = await fetch(`${API_BASE}/api${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) throw new Error(`${res.status}`);
  return res.json();
}

type AdminTab = "stats" | "users" | "purchases" | "messages";

export default function AdminPanel() {
  const [tab, setTab] = useState<AdminTab>("stats");

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      <div className="flex items-center gap-3">
        <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-primary to-primary/60 flex items-center justify-center shadow-md shadow-primary/20">
          <Crown className="w-5 h-5 text-white" />
        </div>
        <div>
          <h1 className="text-2xl font-bold font-serif text-foreground">Admin Dashboard</h1>
          <p className="text-muted-foreground text-xs mt-0.5">Super admin view — manage users, subscriptions and support</p>
        </div>
      </div>

      {/* Tab bar */}
      <div className="flex gap-1 p-1 bg-primary/5 rounded-2xl w-fit border border-primary/10">
        {([
          { id: "stats", label: "Overview", icon: BarChart2 },
          { id: "users", label: "Users", icon: Users },
          { id: "purchases", label: "Purchases", icon: ShoppingBag },
          { id: "messages", label: "Messages", icon: MessageSquare },
        ] as { id: AdminTab; label: string; icon: React.ElementType }[]).map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-semibold transition-all
              ${tab === t.id
                ? "bg-white text-primary shadow-sm shadow-primary/10 border border-primary/15"
                : "text-muted-foreground hover:text-foreground"}`}
          >
            <t.icon className="w-4 h-4" />
            {t.label}
          </button>
        ))}
      </div>

      {tab === "stats" && <StatsTab />}
      {tab === "users" && <UsersTab />}
      {tab === "purchases" && <PurchasesTab />}
      {tab === "messages" && <MessagesTab />}
    </div>
  );
}

function StatsTab() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["admin-stats"],
    queryFn: () => apiFetch("/admin/stats"),
    retry: false,
  });

  const { data: purchases } = useQuery({
    queryKey: ["admin-purchases"],
    queryFn: () => apiFetch("/admin/purchases"),
    retry: false,
  });

  if (isLoading) return <LoadingState />;
  if (error) return <ErrorState error={error} refetch={refetch} />;

  const statCards = [
    { label: "Total Users", value: data.totalUsers, icon: Users, color: "text-primary", bg: "bg-primary/8" },
    { label: "Active Subscriptions", value: data.activeSubscriptions, icon: CheckCircle2, color: "text-green-600", bg: "bg-green-50" },
    { label: "Monthly Revenue", value: `$${data.mrr.toFixed(2)}`, icon: DollarSign, color: "text-primary", bg: "bg-primary/8" },
    { label: "Total Revenue", value: `$${data.totalRevenue.toFixed(2)}`, icon: TrendingUp, color: "text-green-600", bg: "bg-green-50" },
    { label: "Total Orders", value: data.totalOrders, icon: ShoppingBag, color: "text-primary", bg: "bg-primary/8" },
  ];

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-4">
        {statCards.map((s) => (
          <div key={s.label} className="glass-card rounded-2xl p-5 border border-primary/10">
            <div className={`w-8 h-8 rounded-xl ${s.bg} flex items-center justify-center mb-3`}>
              <s.icon className={`w-4 h-4 ${s.color}`} />
            </div>
            <div className={`text-2xl font-bold font-serif ${s.color}`}>{s.value}</div>
            <div className="text-xs text-muted-foreground font-medium mt-1">{s.label}</div>
          </div>
        ))}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Plan breakdown */}
        <div className="glass-card rounded-2xl p-6 border border-primary/10">
          <h2 className="font-bold font-serif text-foreground mb-5">Active Subscriptions by Type</h2>
          <div className="space-y-4">
            {[
              { label: "Residential", count: data.planBreakdown.residential, icon: Globe, color: "bg-primary" },
              { label: "Datacenter", count: data.planBreakdown.datacenter, icon: Zap, color: "bg-blue-500" },
              { label: "IPv6", count: data.planBreakdown.ipv6, icon: BarChart2, color: "bg-purple-500" },
            ].map((item) => {
              const total = data.activeSubscriptions || 1;
              const pct = Math.round((item.count / total) * 100);
              return (
                <div key={item.label}>
                  <div className="flex items-center justify-between mb-1.5">
                    <div className="flex items-center gap-2">
                      <item.icon className="w-4 h-4 text-muted-foreground" />
                      <span className="text-sm font-medium text-foreground">{item.label}</span>
                    </div>
                    <span className="text-sm font-bold text-foreground">{item.count}</span>
                  </div>
                  <div className="h-2 bg-primary/8 rounded-full overflow-hidden">
                    <div className={`h-full ${item.color} rounded-full transition-all`} style={{ width: `${pct}%` }} />
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {/* Recent orders */}
        <div className="glass-card rounded-2xl p-6 border border-primary/10">
          <h2 className="font-bold font-serif text-foreground mb-5">Recent Orders</h2>
          <div className="space-y-3">
            {(purchases || []).slice(0, 5).map((p: any) => (
              <div key={p.id} className="flex items-center justify-between py-2 border-b border-primary/8 last:border-0">
                <div>
                  <div className="text-sm font-semibold text-foreground">{p.userName}</div>
                  <div className="text-xs text-muted-foreground">{p.planName}</div>
                </div>
                <div className="text-right">
                  <div className="text-sm font-bold text-foreground">${p.amount}</div>
                  <StatusBadge status={p.status} />
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function UsersTab() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["admin-users"],
    queryFn: () => apiFetch("/admin/users"),
    retry: false,
  });

  const [search, setSearch] = useState("");
  const [sortField, setSortField] = useState<"name" | "email" | "createdAt" | "lastSignIn">("createdAt");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  if (isLoading) return <LoadingState />;
  if (error) return <ErrorState error={error} refetch={refetch} />;

  const filtered = (data || [])
    .filter((u: any) =>
      !search ||
      u.email?.toLowerCase().includes(search.toLowerCase()) ||
      u.name?.toLowerCase().includes(search.toLowerCase())
    )
    .sort((a: any, b: any) => {
      const av = a[sortField] ?? "";
      const bv = b[sortField] ?? "";
      return sortDir === "asc" ? av.localeCompare(bv) : bv.localeCompare(av);
    });

  function toggleSort(field: typeof sortField) {
    if (sortField === field) setSortDir(sortDir === "asc" ? "desc" : "asc");
    else { setSortField(field); setSortDir("asc"); }
  }

  function SortIcon({ field }: { field: typeof sortField }) {
    if (sortField !== field) return <ChevronDown className="w-3 h-3 opacity-30" />;
    return sortDir === "asc" ? <ChevronDown className="w-3 h-3 text-primary rotate-180" /> : <ChevronDown className="w-3 h-3 text-primary" />;
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
          <input
            type="text"
            placeholder="Search by name or email..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-9 pr-4 py-2.5 rounded-xl border border-primary/15 bg-white text-sm text-foreground w-72 focus:outline-none focus:border-primary focus:ring-1 focus:ring-primary/20"
          />
        </div>
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Users className="w-4 h-4" />
          <span>{filtered.length} user{filtered.length !== 1 ? "s" : ""}</span>
        </div>
      </div>

      <div className="glass-card rounded-2xl border border-primary/10 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-primary/10 bg-primary/3">
                {[
                  { key: "name", label: "Name" },
                  { key: "email", label: "Email" },
                  { key: "createdAt", label: "Joined" },
                  { key: "lastSignIn", label: "Last Sign-in" },
                ].map((col) => (
                  <th
                    key={col.key}
                    onClick={() => toggleSort(col.key as typeof sortField)}
                    className="px-5 py-3 text-left text-xs font-semibold text-muted-foreground uppercase tracking-wider cursor-pointer hover:text-foreground transition-colors select-none"
                  >
                    <div className="flex items-center gap-1.5">
                      {col.label}
                      <SortIcon field={col.key as typeof sortField} />
                    </div>
                  </th>
                ))}
                <th className="px-5 py-3 text-left text-xs font-semibold text-muted-foreground uppercase tracking-wider">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-primary/8">
              {filtered.length === 0 ? (
                <tr>
                  <td colSpan={5} className="px-5 py-12 text-center text-muted-foreground text-sm">No users found</td>
                </tr>
              ) : (
                filtered.map((u: any) => (
                  <tr key={u.id} className="hover:bg-primary/3 transition-colors">
                    <td className="px-5 py-3.5">
                      <div className="flex items-center gap-3">
                        <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-primary/30 to-primary/10 flex items-center justify-center text-xs font-bold text-primary flex-shrink-0">
                          {(u.name || u.email || "?")[0].toUpperCase()}
                        </div>
                        <div>
                          <div className="text-sm font-semibold text-foreground">{u.name || "—"}</div>
                          {u.email === "khemiri.mohamed.ensi@gmail.com" && (
                            <div className="flex items-center gap-1 mt-0.5">
                              <Crown className="w-3 h-3 text-primary" />
                              <span className="text-xs text-primary font-semibold">Super Admin</span>
                            </div>
                          )}
                        </div>
                      </div>
                    </td>
                    <td className="px-5 py-3.5 text-sm text-foreground">{u.email}</td>
                    <td className="px-5 py-3.5 text-sm text-muted-foreground">{formatDate(u.createdAt)}</td>
                    <td className="px-5 py-3.5 text-sm text-muted-foreground">{u.lastSignIn ? formatDate(u.lastSignIn) : "Never"}</td>
                    <td className="px-5 py-3.5">
                      {u.banned
                        ? <span className="text-xs font-semibold text-red-600 bg-red-50 border border-red-200 px-2 py-0.5 rounded-full flex items-center gap-1 w-fit"><Ban className="w-3 h-3" /> Banned</span>
                        : <span className="text-xs font-semibold text-green-700 bg-green-50 border border-green-200 px-2 py-0.5 rounded-full flex items-center gap-1 w-fit"><CheckCircle2 className="w-3 h-3" /> Active</span>}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function PurchasesTab() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["admin-purchases"],
    queryFn: () => apiFetch("/admin/purchases"),
    retry: false,
  });

  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | "active" | "expired" | "cancelled">("all");

  if (isLoading) return <LoadingState />;
  if (error) return <ErrorState error={error} refetch={refetch} />;

  const filtered = (data || []).filter((p: any) =>
    (statusFilter === "all" || p.status === statusFilter) &&
    (!search ||
      p.userEmail?.toLowerCase().includes(search.toLowerCase()) ||
      p.userName?.toLowerCase().includes(search.toLowerCase()) ||
      p.planName?.toLowerCase().includes(search.toLowerCase()))
  );

  const totalRevenue = filtered.reduce((sum: number, p: any) => sum + p.amount, 0);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
            <input
              type="text"
              placeholder="Search orders..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="pl-9 pr-4 py-2.5 rounded-xl border border-primary/15 bg-white text-sm text-foreground w-60 focus:outline-none focus:border-primary focus:ring-1 focus:ring-primary/20"
            />
          </div>
          <div className="flex gap-1 p-1 bg-primary/5 rounded-xl border border-primary/10">
            {(["all", "active", "expired", "cancelled"] as const).map((s) => (
              <button
                key={s}
                onClick={() => setStatusFilter(s)}
                className={`px-3 py-1.5 rounded-lg text-xs font-semibold capitalize transition-all
                  ${statusFilter === s ? "bg-white text-primary shadow-sm border border-primary/15" : "text-muted-foreground hover:text-foreground"}`}
              >
                {s}
              </button>
            ))}
          </div>
        </div>
        <div className="text-sm font-semibold text-foreground">
          {filtered.length} orders · <span className="text-primary">${totalRevenue.toFixed(2)}</span>
        </div>
      </div>

      <div className="glass-card rounded-2xl border border-primary/10 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-primary/10 bg-primary/3">
                {["Order ID", "Customer", "Plan", "Type", "Amount", "Status", "Date"].map((h) => (
                  <th key={h} className="px-5 py-3 text-left text-xs font-semibold text-muted-foreground uppercase tracking-wider">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-primary/8">
              {filtered.length === 0 ? (
                <tr>
                  <td colSpan={7} className="px-5 py-12 text-center text-muted-foreground text-sm">No orders found</td>
                </tr>
              ) : (
                filtered.map((p: any) => (
                  <tr key={p.id} className="hover:bg-primary/3 transition-colors">
                    <td className="px-5 py-3.5 text-xs font-mono text-muted-foreground">{p.id}</td>
                    <td className="px-5 py-3.5">
                      <div className="text-sm font-semibold text-foreground">{p.userName}</div>
                      <div className="text-xs text-muted-foreground">{p.userEmail}</div>
                    </td>
                    <td className="px-5 py-3.5 text-sm text-foreground">{p.planName}</td>
                    <td className="px-5 py-3.5">
                      <span className={`text-xs font-semibold px-2 py-0.5 rounded-full capitalize
                        ${p.planType === "residential" ? "bg-primary/8 text-primary" :
                          p.planType === "datacenter" ? "bg-blue-50 text-blue-600" :
                          "bg-purple-50 text-purple-600"}`}>
                        {p.planType}
                      </span>
                    </td>
                    <td className="px-5 py-3.5 text-sm font-bold text-foreground">${p.amount}</td>
                    <td className="px-5 py-3.5"><StatusBadge status={p.status} /></td>
                    <td className="px-5 py-3.5 text-sm text-muted-foreground">{formatDate(p.createdAt)}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function MessagesTab() {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const [replyText, setReplyText] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | "open" | "replied" | "closed">("all");

  const { data: messages = [], isLoading, error, refetch } = useQuery({
    queryKey: ["admin-messages"],
    queryFn: () => apiFetch("/admin/messages"),
    retry: false,
    refetchInterval: 15000,
  });

  const replyMutation = useMutation({
    mutationFn: ({ id, body }: { id: string; body: string }) =>
      apiFetch(`/admin/messages/${id}/reply`, { method: "POST", body: JSON.stringify({ body }) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-messages"] });
      setReplyText("");
    },
  });

  const closeMutation = useMutation({
    mutationFn: (id: string) =>
      apiFetch(`/admin/messages/${id}/status`, { method: "PATCH", body: JSON.stringify({ status: "closed" }) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-messages"] }),
  });

  const reopenMutation = useMutation({
    mutationFn: (id: string) =>
      apiFetch(`/admin/messages/${id}/status`, { method: "PATCH", body: JSON.stringify({ status: "open" }) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-messages"] }),
  });

  if (isLoading) return <LoadingState />;
  if (error) return <ErrorState error={error} refetch={refetch} />;

  const filtered = messages.filter((m: any) => statusFilter === "all" || m.status === statusFilter);
  const activeMsg = selected ? messages.find((m: any) => m.id === selected) : null;

  const openCount = messages.filter((m: any) => m.status === "open").length;

  return (
    <div className="flex gap-5 h-[600px]">
      {/* Message list */}
      <div className="w-80 flex-shrink-0 flex flex-col glass-card rounded-2xl border border-primary/10 overflow-hidden">
        {/* Header */}
        <div className="px-4 py-3 border-b border-primary/10 flex items-center justify-between flex-shrink-0">
          <div className="flex items-center gap-2">
            <Inbox className="w-4 h-4 text-primary" />
            <span className="text-sm font-bold text-foreground">Inbox</span>
            {openCount > 0 && (
              <span className="text-xs font-bold bg-primary text-white px-1.5 py-0.5 rounded-full">{openCount}</span>
            )}
          </div>
          <button onClick={() => refetch()} className="text-muted-foreground hover:text-primary transition-colors">
            <RefreshCw className="w-3.5 h-3.5" />
          </button>
        </div>

        {/* Filter tabs */}
        <div className="flex border-b border-primary/10 flex-shrink-0">
          {(["all", "open", "replied", "closed"] as const).map((s) => (
            <button
              key={s}
              onClick={() => setStatusFilter(s)}
              className={`flex-1 py-2 text-xs font-semibold capitalize transition-colors
                ${statusFilter === s ? "text-primary border-b-2 border-primary" : "text-muted-foreground hover:text-foreground"}`}
            >
              {s}
            </button>
          ))}
        </div>

        {/* List */}
        <div className="flex-1 overflow-y-auto divide-y divide-primary/8">
          {filtered.length === 0 ? (
            <div className="py-12 text-center text-sm text-muted-foreground">No messages</div>
          ) : (
            filtered.map((m: any) => (
              <button
                key={m.id}
                onClick={() => setSelected(m.id)}
                className={`w-full text-left px-4 py-3.5 hover:bg-primary/5 transition-colors
                  ${selected === m.id ? "bg-primary/8 border-r-2 border-primary" : ""}`}
              >
                <div className="flex items-start justify-between gap-2 mb-1">
                  <div className="flex items-center gap-1.5 min-w-0">
                    {m.status === "open" && <span className="w-1.5 h-1.5 rounded-full bg-primary flex-shrink-0 mt-0.5" />}
                    <span className="text-xs font-bold text-foreground truncate">{m.userName || m.userEmail}</span>
                  </div>
                  <MsgStatusBadge status={m.status} />
                </div>
                <div className="text-xs font-semibold text-foreground/80 truncate mb-0.5">{m.subject}</div>
                <div className="text-xs text-muted-foreground truncate">{m.body}</div>
                <div className="text-xs text-muted-foreground/60 mt-1">{formatDate(m.createdAt)}</div>
              </button>
            ))
          )}
        </div>
      </div>

      {/* Conversation view */}
      <div className="flex-1 flex flex-col glass-card rounded-2xl border border-primary/10 overflow-hidden">
        {!activeMsg ? (
          <div className="flex-1 flex flex-col items-center justify-center text-center p-8">
            <MessageSquare className="w-12 h-12 text-primary/20 mb-3" />
            <p className="text-sm font-semibold text-foreground/60">Select a message to view the conversation</p>
          </div>
        ) : (
          <>
            {/* Conversation header */}
            <div className="px-6 py-4 border-b border-primary/10 flex items-start justify-between gap-4 flex-shrink-0">
              <div>
                <h3 className="font-bold text-foreground">{activeMsg.subject}</h3>
                <div className="flex items-center gap-2 mt-1">
                  <span className="text-xs text-muted-foreground">From:</span>
                  <span className="text-xs font-semibold text-foreground">{activeMsg.userName}</span>
                  <span className="text-xs text-muted-foreground">·</span>
                  <span className="text-xs text-muted-foreground">{activeMsg.userEmail}</span>
                  <span className="text-xs text-muted-foreground">·</span>
                  <span className="text-xs text-muted-foreground">{formatDate(activeMsg.createdAt)}</span>
                </div>
              </div>
              <div className="flex items-center gap-2 flex-shrink-0">
                <MsgStatusBadge status={activeMsg.status} />
                {activeMsg.status !== "closed" ? (
                  <button
                    onClick={() => closeMutation.mutate(activeMsg.id)}
                    disabled={closeMutation.isPending}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold border border-gray-200 text-muted-foreground hover:border-gray-400 hover:text-foreground transition-colors"
                  >
                    <X className="w-3 h-3" />
                    Close
                  </button>
                ) : (
                  <button
                    onClick={() => reopenMutation.mutate(activeMsg.id)}
                    disabled={reopenMutation.isPending}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold border border-primary/20 text-primary hover:bg-primary/5 transition-colors"
                  >
                    <RefreshCw className="w-3 h-3" />
                    Reopen
                  </button>
                )}
              </div>
            </div>

            {/* Messages */}
            <div className="flex-1 overflow-y-auto p-6 space-y-4">
              {/* User message */}
              <div className="flex gap-3">
                <div className="w-8 h-8 rounded-full bg-gradient-to-br from-primary/30 to-primary/10 flex items-center justify-center text-xs font-bold text-primary flex-shrink-0 mt-0.5">
                  {(activeMsg.userName || activeMsg.userEmail || "?")[0].toUpperCase()}
                </div>
                <div className="flex-1">
                  <div className="flex items-center gap-2 mb-1.5">
                    <span className="text-xs font-bold text-foreground">{activeMsg.userName || activeMsg.userEmail}</span>
                    <span className="text-xs text-muted-foreground">{formatDate(activeMsg.createdAt)}</span>
                  </div>
                  <div className="bg-primary/5 border border-primary/10 rounded-2xl rounded-tl-sm px-4 py-3">
                    <p className="text-sm text-foreground leading-relaxed whitespace-pre-wrap">{activeMsg.body}</p>
                  </div>
                </div>
              </div>

              {/* Admin reply */}
              {activeMsg.reply && (
                <div className="flex gap-3 flex-row-reverse">
                  <div className="w-8 h-8 rounded-full bg-gradient-to-br from-primary to-primary/60 flex items-center justify-center flex-shrink-0 mt-0.5">
                    <Crown className="w-4 h-4 text-white" />
                  </div>
                  <div className="flex-1">
                    <div className="flex items-center gap-2 mb-1.5 flex-row-reverse">
                      <span className="text-xs font-bold text-foreground">You (Admin)</span>
                      <span className="text-xs text-muted-foreground">{formatDate(activeMsg.reply.createdAt)}</span>
                    </div>
                    <div className="bg-gradient-to-br from-primary/10 to-primary/5 border border-primary/15 rounded-2xl rounded-tr-sm px-4 py-3">
                      <p className="text-sm text-foreground leading-relaxed whitespace-pre-wrap">{activeMsg.reply.body}</p>
                    </div>
                    <div className="flex items-center gap-1 mt-2 justify-end">
                      <Check className="w-3 h-3 text-primary" />
                      <span className="text-xs text-primary font-medium">Reply sent</span>
                    </div>
                  </div>
                </div>
              )}
            </div>

            {/* Reply composer */}
            {activeMsg.status !== "closed" && (
              <div className="px-6 py-4 border-t border-primary/10 flex-shrink-0">
                {activeMsg.reply && (
                  <p className="text-xs text-muted-foreground mb-3 flex items-center gap-1.5">
                    <RefreshCw className="w-3 h-3" />
                    Send an updated reply to overwrite the previous one
                  </p>
                )}
                <div className="flex gap-3 items-end">
                  <textarea
                    value={replyText}
                    onChange={(e) => setReplyText(e.target.value)}
                    placeholder={`Reply to ${activeMsg.userName || activeMsg.userEmail}...`}
                    rows={3}
                    className="flex-1 px-4 py-3 rounded-xl border border-primary/15 bg-white text-sm text-foreground resize-none focus:outline-none focus:border-primary focus:ring-1 focus:ring-primary/20"
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && replyText.trim()) {
                        replyMutation.mutate({ id: activeMsg.id, body: replyText });
                      }
                    }}
                  />
                  <button
                    onClick={() => replyText.trim() && replyMutation.mutate({ id: activeMsg.id, body: replyText })}
                    disabled={!replyText.trim() || replyMutation.isPending}
                    className="flex-shrink-0 w-11 h-11 rounded-xl gold-button text-white flex items-center justify-center disabled:opacity-50 transition-all hover:scale-105 active:scale-95"
                  >
                    {replyMutation.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
                  </button>
                </div>
                {replyMutation.isError && (
                  <p className="text-xs text-red-600 mt-2">Failed to send reply — try again.</p>
                )}
                {replyMutation.isSuccess && !replyText && (
                  <p className="text-xs text-green-600 mt-2 flex items-center gap-1">
                    <Check className="w-3 h-3" /> Reply sent successfully
                  </p>
                )}
                <p className="text-xs text-muted-foreground/60 mt-2">Press Cmd/Ctrl+Enter to send</p>
              </div>
            )}

            {activeMsg.status === "closed" && (
              <div className="px-6 py-4 border-t border-primary/10 bg-gray-50 flex-shrink-0">
                <p className="text-xs text-muted-foreground text-center">This conversation is closed. Reopen it to reply.</p>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function MsgStatusBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; cls: string }> = {
    open: { label: "Open", cls: "bg-primary/10 text-primary border-primary/20" },
    replied: { label: "Replied", cls: "bg-green-50 text-green-700 border-green-200" },
    closed: { label: "Closed", cls: "bg-gray-100 text-gray-500 border-gray-200" },
  };
  const cfg = map[status] ?? map.closed;
  return (
    <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full border flex-shrink-0 ${cfg.cls}`}>{cfg.label}</span>
  );
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { icon: React.ElementType; label: string; cls: string }> = {
    active: { icon: CheckCircle2, label: "Active", cls: "bg-green-50 text-green-700 border-green-200" },
    expired: { icon: Clock, label: "Expired", cls: "bg-gray-50 text-gray-500 border-gray-200" },
    cancelled: { icon: XCircle, label: "Cancelled", cls: "bg-red-50 text-red-600 border-red-200" },
  };
  const cfg = map[status] ?? map.expired;
  return (
    <span className={`text-xs font-semibold px-2 py-0.5 rounded-full border flex items-center gap-1 w-fit ${cfg.cls}`}>
      <cfg.icon className="w-3 h-3" />
      {cfg.label}
    </span>
  );
}

function formatDate(iso: string) {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

function LoadingState() {
  return (
    <div className="flex items-center justify-center py-24">
      <div className="text-center">
        <RefreshCw className="w-8 h-8 text-primary animate-spin mx-auto mb-3" />
        <p className="text-sm text-muted-foreground">Loading admin data...</p>
      </div>
    </div>
  );
}

function ErrorState({ error, refetch }: { error: unknown; refetch: () => void }) {
  return (
    <div className="glass-card rounded-2xl p-10 border border-red-200 text-center">
      <XCircle className="w-10 h-10 text-red-400 mx-auto mb-3" />
      <h3 className="font-bold text-foreground mb-1">Failed to load data</h3>
      <p className="text-sm text-muted-foreground mb-5">
        {(error as Error)?.message === "403"
          ? "Access denied — super admin only."
          : "Could not connect to the server. Try again."}
      </p>
      <button onClick={refetch} className="px-5 py-2 rounded-xl text-sm font-bold gold-button text-white">
        Retry
      </button>
    </div>
  );
}
