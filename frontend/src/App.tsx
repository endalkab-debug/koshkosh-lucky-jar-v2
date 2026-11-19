import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api";
import { Trophy, Ticket, History, ShieldCheck, RefreshCw, X, Sparkles } from "lucide-react";

type NumberItem = { number: number; status: "available" | "reserved" | "sold" };

function ReceiptPreview({ orderId }: { orderId: string }) {
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  async function view() {
    try { const result = await api.adminReceiptUrl(orderId); setUrl(result.url); }
    catch (e) { setError(e instanceof Error ? e.message : "Receipt preview unavailable."); }
  }
  return <div className="receipt-preview"><button className="secondary" onClick={()=>void view()}>View private receipt</button>{url && <a href={url} target="_blank" rel="noreferrer">Open receipt (expires in 5 minutes)</a>}{error && <small className="muted">{error}</small>}</div>;
}

export default function App() {
  const [data, setData] = useState<any>(null);
  const [numbers, setNumbers] = useState<NumberItem[]>([]);
  const [cart, setCart] = useState<any>(null);
  const [tab, setTab] = useState("play");
  const [history, setHistory] = useState<any>(null);
  const [orders, setOrders] = useState<any[]>([]);
  const [busy, setBusy] = useState<number | string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [checkoutStep, setCheckoutStep] = useState<"closed" | "name" | "phone" | "invoice">("closed");
  const [customerName, setCustomerName] = useState("");
  const [customerPhone, setCustomerPhone] = useState("");
  const [invoice, setInvoice] = useState<any>(null);
  const [receiptFile, setReceiptFile] = useState<File | null>(null);
  const [receiptBusy, setReceiptBusy] = useState(false);
  const [receiptSubmitted, setReceiptSubmitted] = useState(false);

  const load = useCallback(async () => {
    setError("");
    try {
      const [boot, board] = await Promise.all([api.bootstrap(), api.board()]);
      setData(boot); setCart(boot.cart); setNumbers(board.numbers);
      if (boot.pending_order) {
        setInvoice(boot.pending_order);
        setCheckoutStep("invoice");
        setReceiptSubmitted(Boolean(boot.pending_order.receipt_submitted));
      } else if (boot.cart?.state === "awaiting_name") setCheckoutStep("name");
      else if (boot.cart?.state === "awaiting_phone") setCheckoutStep("phone");
    } catch (e) { setError(e instanceof Error ? e.message : "Could not connect to KoshKosh."); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const selected = useMemo(() => new Set<number>(cart?.numbers || []), [cart]);
  const total = (cart?.numbers?.length || 0) * (data?.game?.price_per_number || 0);
  const lang = data?.user?.language || "en";
  const t = (en: string, am: string) => lang === "am" ? am : en;

  async function toggle(number: number) {
    setBusy(number); setError("");
    try { const result = await api.toggle(number); setCart(result.cart); await loadBoardOnly(); window.Telegram?.WebApp?.HapticFeedback?.impactOccurred("light"); }
    catch (e) { setError(e instanceof Error ? e.message : "Selection failed."); await loadBoardOnly(); }
    finally { setBusy(null); }
  }
  async function loadBoardOnly() { const board = await api.board(); setNumbers(board.numbers); }
  async function cancel() {
    setBusy("cancel");
    try { await api.cancel(); setCart(null); await loadBoardOnly(); setNotice(t("Your selection was released.", "ምርጫዎ ተለቋል።")); }
    catch (e) { setError(e instanceof Error ? e.message : "Could not cancel selection."); }
    finally { setBusy(null); }
  }
  async function startCheckout() {
    setBusy("checkout"); setError("");
    try { await api.checkoutStart(); setCheckoutStep("name"); }
    catch (e) { setError(e instanceof Error ? e.message : "Could not start checkout."); }
    finally { setBusy(null); }
  }
  async function submitCustomerName() {
    setBusy("checkout-name"); setError("");
    try { await api.checkoutName(customerName); setCheckoutStep("phone"); }
    catch (e) { setError(e instanceof Error ? e.message : "Could not save your name."); }
    finally { setBusy(null); }
  }
  async function submitCustomerPhone() {
    setBusy("checkout-phone"); setError("");
    try { const result = await api.checkoutPhone(customerPhone); setInvoice(result.invoice); setReceiptFile(null); setReceiptSubmitted(false); setCheckoutStep("invoice"); setCart(null); await loadBoardOnly(); }
    catch (e) { setError(e instanceof Error ? e.message : "Could not create your invoice."); }
    finally { setBusy(null); }
  }
  async function submitReceipt() {
    if (!invoice || !receiptFile) return;
    setReceiptBusy(true); setError("");
    try { await api.uploadReceipt(invoice.order_id, receiptFile); setReceiptSubmitted(true); setNotice(t("Receipt uploaded. The host will verify your payment.", "ደረሰኙ ተልኳል። አስተናጋጁ ክፍያዎን ያረጋግጣል።")); window.Telegram?.WebApp?.HapticFeedback?.notificationOccurred("success"); }
    catch (e) { setError(e instanceof Error ? e.message : "Receipt upload failed."); }
    finally { setReceiptBusy(false); }
  }
  async function openHistory() { setTab("history"); try { setHistory(await api.history()); } catch (e) { setError(e instanceof Error ? e.message : "History unavailable."); } }
  async function openAdmin() { setTab("admin"); try { const r = await api.adminOrders(); setOrders(r.orders); } catch (e) { setError(e instanceof Error ? e.message : "Admin access unavailable."); } }
  async function decide(id: string, decision: "approve" | "reject") {
    setBusy(id); try { await api.decide(id, decision); const r = await api.adminOrders(); setOrders(r.orders); setNotice(`Order ${id} ${decision}d.`); }
    catch (e) { setError(e instanceof Error ? e.message : "Decision failed."); } finally { setBusy(null); }
  }

  if (!data && !error) return <main className="shell loading"><div className="jar-mark">✦</div><p>Opening your lucky jar…</p></main>;
  const isAdmin = Boolean(data?.user?.is_admin);

  return <main className="shell">
    <header className="topbar"><div className="brand"><span className="brand-icon">✦</span><div><b>KOSHKOSH</b><small>LUCKY JAR</small></div></div><span className="round">{data?.game?.round_id || "KLR"}</span></header>
    <section className="hero"><div className="eyebrow"><Sparkles size={13}/> {t("YOUR NEXT LUCKY MOMENT", "የእድልዎ ቀጣይ ቅጽበት")}</div><h1>{t("Choose your lucky numbers.", "የዕድል ቁጥሮችዎን ይምረጡ።")}</h1><p>{t("One number. One chance. A jar full of possibilities.", "አንድ ቁጥር። አንድ ዕድል። በተስፋ የተሞላ ማሰሮ።")}</p></section>
    <section className="stats"><div><strong>{data?.game?.counts?.available ?? "—"}</strong><span>{t("Available", "ያልተያዙ")}</span></div><div><strong>{data?.game?.counts?.reserved ?? "—"}</strong><span>{t("Reserved", "የተያዙ")}</span></div><div><strong>{data?.game?.counts?.sold ?? "—"}</strong><span>{t("Sold", "የተሸጡ")}</span></div></section>
    {error && <div className="alert error">{error}<button onClick={() => setError("")}><X size={15}/></button></div>}
    {notice && <div className="alert success">{notice}<button onClick={() => setNotice("")}><X size={15}/></button></div>}
    <nav className="tabs"><button className={tab === "play" ? "active" : ""} onClick={() => setTab("play")}><Ticket size={16}/>{t("Play", "ይጫወቱ")}</button><button className={tab === "history" ? "active" : ""} onClick={() => void openHistory()}><History size={16}/>{t("History", "ታሪክ")}</button>{isAdmin && <button className={tab === "admin" ? "active" : ""} onClick={() => void openAdmin()}><ShieldCheck size={16}/>Admin</button>}</nav>
    {tab === "play" && <>
      <div className="section-heading"><div><h2>{t("Number board", "የቁጥሮች ሰንጠረዥ")}</h2><p>{t("Tap available numbers to reserve them.", "ለመያዝ ያልተያዙ ቁጥሮችን ይንኩ።")}</p></div><button className="icon-button" onClick={() => void load()} aria-label="Refresh"><RefreshCw size={17}/></button></div>
      <div className="legend"><span><i className="dot available"/>{t("Available", "ያልተያዙ")}</span><span><i className="dot selected"/>{t("Your pick", "የመረጡት")}</span><span><i className="dot reserved"/>{t("Reserved", "የተያዙ")}</span><span><i className="dot sold"/>{t("Sold", "የተሸጡ")}</span></div>
      <div className="number-grid">{numbers.map(item => { const mine = selected.has(item.number); const disabled = item.status !== "available" && !mine; return <button key={item.number} disabled={disabled || busy === item.number} onClick={() => void toggle(item.number)} className={`number ${mine ? "mine" : item.status}`} aria-label={`Number ${item.number}, ${mine ? "selected" : item.status}`}>{item.number}</button>; })}</div>
      <section className="cart-card"><div className="cart-top"><div><span className="cart-label">{t("YOUR SELECTION", "የመረጡት")}</span><strong>{cart?.numbers?.length || 0} <small>{t("numbers", "ቁጥሮች")}</small></strong></div><div className="cart-total"><span>{t("TOTAL", "ጠቅላላ")}</span><strong>{total.toLocaleString()} <small>ETB</small></strong></div></div>
        {cart?.numbers?.length ? <><p className="expires">{t("Reservation expires in", "የተያዘው ጊዜ የሚያበቃው")}: {Math.floor((cart.remaining_seconds || 0) / 60)}m {(cart.remaining_seconds || 0) % 60}s</p>{cart.state !== "awaiting_payment" && <><button className="primary full" disabled={busy === "checkout"} onClick={() => void startCheckout()}>{busy === "checkout" ? t("Preparing checkout…", "ቼክአውት በማዘጋጀት ላይ…") : t("Continue to checkout", "ወደ ክፍያ ይቀጥሉ")}</button><button className="secondary full" disabled={busy === "cancel"} onClick={() => void cancel()}>{t("Release selection", "ምርጫውን ይልቀቁ")}</button></>}</> : <p className="empty-cart">{t("Your selected numbers will appear here.", "የመረጡት ቁጥሮች እዚህ ይታያሉ።")}</p>}
        {checkoutStep === "name" && <div className="checkout-panel"><h3>{t("Your full name", "ሙሉ ስምዎ")}</h3><input value={customerName} onChange={e=>setCustomerName(e.target.value)} maxLength={100} placeholder={t("Enter your full name", "ሙሉ ስምዎን ያስገቡ")}/><button className="primary full" disabled={busy === "checkout-name" || customerName.trim().length < 2} onClick={()=>void submitCustomerName()}>{t("Continue", "ይቀጥሉ")}</button></div>}
        {checkoutStep === "phone" && <div className="checkout-panel"><h3>{t("Phone number", "ስልክ ቁጥር")}</h3><input value={customerPhone} onChange={e=>setCustomerPhone(e.target.value)} inputMode="tel" placeholder="09XXXXXXXX"/><button className="primary full" disabled={busy === "checkout-phone" || customerPhone.trim().length < 9} onClick={()=>void submitCustomerPhone()}>{t("Generate invoice", "ደረሰኝ ይፍጠሩ")}</button></div>}
        {checkoutStep === "invoice" && invoice && <div className="checkout-panel invoice-panel"><div className="invoice-head"><span>{t("PAYMENT INVOICE", "የክፍያ ደረሰኝ")}</span><b>{invoice.order_id}</b></div><p>{t("Round", "ዙር")}: {invoice.round_id}</p><p>{t("Numbers", "ቁጥሮች")}: {invoice.numbers.join(", ")}</p><p>{t("Name", "ስም")}: {invoice.customer_name}</p><p>{t("Phone", "ስልክ")}: {invoice.customer_phone}</p><div className="invoice-total">{Number(invoice.total).toLocaleString()} ETB</div><p className="expires">{t("Pay before", "ከዚህ ጊዜ በፊት ይክፈሉ")}: {invoice.expires_at_ethiopia}</p><div className="payment-accounts"><b>CBE</b><span>{invoice.payment_accounts.cbe}</span><b>Telebirr</b><span>{invoice.payment_accounts.telebirr}</span><b>CBEBirr</b><span>{invoice.payment_accounts.cbebirr}</span></div>{receiptSubmitted ? <p className="receipt-success">✓ {t("Receipt submitted successfully. Await host verification.", "ደረሰኙ ተልኳል። የአስተናጋጁን ማረጋገጫ ይጠብቁ።")}</p> : <><label className="upload-label">{t("Upload payment screenshot", "የክፍያ ማስረጃ ይስቀሉ")}<input type="file" accept="image/jpeg,image/png,image/webp" onChange={e=>setReceiptFile(e.target.files?.[0] || null)}/></label>{receiptFile && <small className="muted">{receiptFile.name} · {(receiptFile.size/1024/1024).toFixed(2)} MB</small>}<button className="primary full" disabled={!receiptFile || receiptBusy} onClick={()=>void submitReceipt()}>{receiptBusy ? t("Uploading securely…", "በደህና በመላክ ላይ…") : t("Submit payment receipt", "የክፍያ ደረሰኝ ይላኩ")}</button></>}<p className="hint">{t("Your receipt is stored privately. Only authorized administrators can view it.", "ደረሰኙ በግል ይቀመጣል። የተፈቀደላቸው አስተዳዳሪዎች ብቻ ሊያዩት ይችላሉ።")}</p></div>}
      </section>
      <section className="prizes"><div className="section-heading"><div><h2>{t("The prizes", "ሽልማቶች")}</h2><p>{t("Three winners every round", "በየዙሩ ሶስት አሸናፊዎች")}</p></div><Trophy className="gold"/></div><div className="prize-row">{(data?.game?.prizes || []).map((p:any) => <div className="prize" key={p.rank}><span>{["🥇","🥈","🥉"][p.rank-1]}</span><b>{Number(p.amount).toLocaleString()} ETB</b><small>{p.name}</small></div>)}</div></section>
    </>}
    {tab === "history" && <section className="panel"><h2>{t("Your history", "የእርስዎ ታሪክ")}</h2>{!history ? <p>Loading…</p> : <><h3>{t("Tickets", "ቲኬቶች")}</h3>{history.tickets.length ? history.tickets.map((x:any) => <p className="history-row" key={x.order_id}>#{x.order_id} · {x.numbers.join(", ")} · {x.total} ETB</p>) : <p className="muted">{t("No approved tickets yet.", "እስካሁን የጸደቁ ቲኬቶች የሉም።")}</p>}<h3>{t("Your wins", "ያሸነፉት")}</h3>{history.wins.length ? history.wins.map((x:any,i:number) => <p className="history-row" key={`${x.round_id}-${i}`}>{x.prize_name} · {Number(x.amount).toLocaleString()} ETB · #{x.number}</p>) : <p className="muted">{t("No wins recorded yet.", "እስካሁን ያሸነፉት የለም።")}</p>}</>}</section>}
    {tab === "admin" && <section className="panel"><h2>Payment approvals</h2>{orders.length ? orders.map(o => <article className="order" key={o.order_id}><b>Order {o.order_id}</b><p>{o.customer_name} · {o.customer_phone}</p><p>Numbers: {o.numbers.join(", ")} · {Number(o.total).toLocaleString()} ETB</p>{o.has_receipt ? <ReceiptPreview orderId={o.order_id}/> : <p className="muted">Receipt not uploaded yet.</p>}<div className="order-actions"><button className="approve" disabled={busy === o.order_id} onClick={() => void decide(o.order_id,"approve")}>Approve</button><button className="reject" disabled={busy === o.order_id} onClick={() => void decide(o.order_id,"reject")}>Reject</button></div></article>) : <p className="muted">No pending orders.</p>}</section>}
    <footer>© KoshKosh Lucky Jar <span>·</span> {t("Play responsibly", "በኃላፊነት ይጫወቱ")}</footer>
  </main>;
}
