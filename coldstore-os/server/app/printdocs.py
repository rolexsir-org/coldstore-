"""Server-rendered printable documents (intake receipt, dispatch note, invoice,
owner statement). Inline CSS only — must print correctly on a LAN with no internet."""
from __future__ import annotations

from html import escape

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (Chamber, DispatchLine, DispatchNote, Grade, IntakeReceipt,
                     Invoice, InvoiceLine, Lot, OutwardOrderLine, Package, Party,
                     QualityInspectionItem, QualityCheck, QualityPhoto, Season, Variety)

_CSS = """
<style>
 body{font-family:'Segoe UI',Arial,sans-serif;color:#111;max-width:800px;margin:24px auto;padding:0 16px}
 h1{font-size:20px;margin:0} .sub{color:#555;font-size:12px;margin:2px 0 14px}
 table{border-collapse:collapse;width:100%;margin:10px 0}
 th,td{border:1px solid #999;padding:6px 8px;font-size:13px;text-align:left}
 th{background:#eee} .right{text-align:right} .tot{font-weight:700}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:2px 24px;font-size:13px;margin:8px 0}
 .box{border:1px solid #999;padding:10px;margin-top:12px;font-size:13px}
 @media print{.noprint{display:none}}
</style>
<button class="noprint" onclick="window.print()">Print</button>
"""


def _page(title: str, body: str) -> str:
    return f"<!doctype html><title>{escape(title)}</title>{_CSS}<h1>{escape(title)}</h1>" \
           f"<div class='sub'>ColdStore OS — CA Apple Edition · printed document</div>{body}"


def _party(db: Session, pid) -> str:
    p = db.get(Party, pid) if pid else None
    return f"{p.code} · {escape(p.name)}" if p else "—"


def receipt_html(db: Session, r: IntakeReceipt) -> str:
    v = db.get(Variety, r.variety_id)
    g = db.get(Grade, r.grade_id)
    pk = db.get(Package, r.packaging_id)
    s = db.get(Season, r.season_id)
    lot = db.scalar(select(Lot).where(Lot.intake_receipt_id == r.id))
    body = f"""
<div class='grid'>
 <div><b>Receipt No:</b> {escape(r.receipt_no)}</div><div><b>Status:</b> {r.status}</div>
 <div><b>Date:</b> {r.received_at:%Y-%m-%d %H:%M}</div><div><b>Season:</b> {escape(s.name) if s else '—'}</div>
 <div><b>Owner:</b> {_party(db, r.owner_id)}</div><div><b>Grower:</b> {_party(db, r.grower_id)}</div>
 <div><b>Agent:</b> {_party(db, r.agent_id)}</div><div><b>Transporter:</b> {_party(db, r.transporter_id)}</div>
 <div><b>Vehicle:</b> {escape(r.vehicle_no)}</div><div><b>Weighbridge ref:</b> {escape(r.weighbridge_ref)}</div>
</div>
<table><tr><th>Variety</th><th>Grade</th><th>Harvest yr</th><th>Packaging</th>
<th class='right'>Units</th><th class='right'>Gross kg</th><th class='right'>Tare kg</th>
<th class='right'>Net kg</th></tr>
<tr><td>{escape(v.name) if v else ''}</td><td>{escape(g.name) if g else ''}</td>
<td>{r.harvest_year}</td><td>{escape(pk.name) if pk else ''}</td>
<td class='right'>{r.unit_count}</td><td class='right'>{float(r.gross_kg):,.3f}</td>
<td class='right'>{float(r.tare_kg):,.3f}</td><td class='right tot'>{float(r.net_kg):,.3f}</td></tr></table>
<div class='box'><b>Condition notes:</b> {escape(r.condition_notes) or '—'}<br>
<b>Lot:</b> {lot.lot_no if lot else 'pending verification'}
{'<br><b>Correction of:</b> ' + str(r.supersedes_id) + ' — ' + escape(r.correction_reason) if r.supersedes_id else ''}</div>
<div class='grid' style='margin-top:36px'><div>Receiver sign: ____________</div>
<div style='text-align:right'>Grower/Agent sign: ____________</div></div>"""
    return _page(f"Intake Receipt {r.receipt_no}", body)


def dispatch_html(db: Session, dn: DispatchNote) -> str:
    lines = db.scalars(select(DispatchLine).where(DispatchLine.dispatch_id == dn.id)).all()
    rows = ""
    total = 0.0
    for l in lines:
        lot = db.get(Lot, l.lot_id)
        ch = db.get(Chamber, l.chamber_id)
        total += float(l.qty_kg)
        rows += (f"<tr><td>{lot.lot_no}</td><td>{ch.code if ch else ''}</td>"
                 f"<td class='right'>{l.unit_count}</td><td class='right'>{float(l.qty_kg):,.3f}</td></tr>")
    body = f"""
<div class='grid'>
 <div><b>Dispatch No:</b> {escape(dn.dispatch_no)}</div><div><b>Status:</b> {dn.status}</div>
 <div><b>Date:</b> {dn.dispatched_at:%Y-%m-%d %H:%M}</div><div><b>Owner:</b> {_party(db, dn.owner_id)}</div>
 <div><b>Released by:</b> {_party(db, dn.released_by_party_id)}</div>
 <div><b>Authorized person:</b> {escape(dn.authorized_person_name)}</div>
 <div><b>ID proof:</b> {escape(dn.id_proof_type)} {escape(dn.id_proof_no)}</div>
 <div><b>Vehicle:</b> {escape(dn.vehicle_no)}</div><div><b>Destination:</b> {escape(dn.destination)}</div>
</div>
<table><tr><th>Lot</th><th>Chamber</th><th class='right'>Units</th><th class='right'>Qty kg</th></tr>
{rows}<tr><td colspan='2' class='tot'>Total</td><td></td>
<td class='right tot'>{total:,.3f}</td></tr></table>
{'<div class="box"><b>VOID:</b> ' + escape(dn.void_reason) + '</div>' if dn.status == 'void' else ''}
<div class='grid' style='margin-top:36px'><div>Issued by: ____________</div>
<div style='text-align:right'>Receiver/Driver sign: ____________</div></div>"""
    return _page(f"Dispatch Note {dn.dispatch_no}", body)


def _money(minor: int) -> str:
    return f"₹{minor / 100:,.2f}"


def invoice_html(db: Session, inv: Invoice) -> str:
    owner = db.get(Party, inv.owner_id)
    lines = db.scalars(select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)
                       .order_by(InvoiceLine.id)).all()
    rows = ""
    for l in lines:
        lot_no = db.get(Lot, l.lot_id).lot_no if l.lot_id else ""
        qty = f"{float(l.qty_kg):,.3f} kg" if l.qty_kg else (f"{l.units} units" if l.units else "—")
        rows += (f"<tr><td>{escape(l.charge_code)}</td><td>{escape(l.description)}</td>"
                 f"<td>{lot_no}</td><td class='right'>{qty}</td>"
                 f"<td class='right'>{('₹' + str(l.rate)) if l.rate else ''}</td>"
                 f"<td class='right'>{_money(l.amount_minor)}</td></tr>")
    tax_rows = "".join(
        f"<tr><td colspan='4'></td><td class='right'>{escape(t['name'])} {t['rate_percent']}%</td>"
        f"<td class='right'>{_money(t['tax_minor'])}</td></tr>" for t in inv.tax_snapshot)
    body = f"""
<div class='grid'>
 <div><b>Invoice No:</b> {escape(inv.invoice_no)}</div><div><b>Status:</b> {inv.status}</div>
 <div><b>Billed to:</b> {owner.code} · {escape(owner.name)}</div>
 <div><b>Period:</b> {inv.period_from} → {inv.period_to}</div>
 <div><b>Tariff:</b> #{inv.tariff_id} v{inv.tariff_version}</div>
 <div><b>Due date:</b> {inv.due_date or '—'}</div>
</div>
<table><tr><th>Charge</th><th>Description</th><th>Lot</th><th class='right'>Qty</th>
<th class='right'>Rate</th><th class='right'>Amount</th></tr>
{rows}
<tr><td colspan='4'></td><td class='right'><b>Subtotal</b></td>
<td class='right'>{_money(inv.subtotal_minor)}</td></tr>
<tr><td colspan='4'></td><td class='right'>Discount</td>
<td class='right'>−{_money(inv.discount_minor)}</td></tr>
{tax_rows}
<tr><td colspan='4'></td><td class='right tot'>Total</td>
<td class='right tot'>{_money(inv.total_minor)}</td></tr></table>
<div class='grid' style='margin-top:36px'><div>Prepared by: ____________</div>
<div style='text-align:right'>For ColdStore (sign/seal): ____________</div></div>"""
    return _page(f"Invoice {inv.invoice_no}", body)


def statement_html(st: dict) -> str:
    inv_rows = "".join(
        f"<tr><td>{i['invoice_no']}</td><td>{i['period_from']} → {i['period_to']}</td>"
        f"<td>{i['status']}</td><td class='right'>{_money(i['total_minor'])}</td>"
        f"<td class='right'>{_money(i['outstanding_minor'])}</td></tr>"
        for i in st["invoices"])
    pay_rows = "".join(
        f"<tr><td>{p['payment_no']}</td><td>{p['received_at'][:10]}</td>"
        f"<td>{p['method']}</td><td class='right'>{_money(p['amount_minor'])}</td></tr>"
        for p in st["payments"])
    body = f"""
<div class='grid'><div><b>Owner:</b> {st['owner']['code']} · {escape(st['owner']['name'])}</div>
<div><b>Generated:</b> {st['generated_at'][:16]}Z</div></div>
<h3>Invoices</h3><table><tr><th>No</th><th>Period</th><th>Status</th>
<th class='right'>Total</th><th class='right'>Outstanding</th></tr>{inv_rows}</table>
<h3>Payments</h3><table><tr><th>No</th><th>Date</th><th>Method</th>
<th class='right'>Amount</th></tr>{pay_rows}</table>
<table><tr><td class='right'><b>Outstanding</b></td><td class='right'>{_money(st['total_outstanding_minor'])}</td></tr>
<tr><td class='right'><b>Credit balance</b></td><td class='right'>{_money(st['credit_minor'])}</td></tr>
<tr><td class='right'><b>Net due</b></td><td class='right tot'>{_money(st['net_due_minor'])}</td></tr></table>"""
    return _page(f"Statement — {st['owner']['code']}", body)


def inspection_html(db: Session, i) -> str:
    lot = db.get(Lot, i.lot_id)
    owner = db.get(Party, lot.owner_id) if lot else None
    items = db.scalars(select(QualityInspectionItem)
                       .where(QualityInspectionItem.inspection_id == i.id)
                       .order_by(QualityInspectionItem.id)).all()
    checks = {c.id: c for c in db.scalars(select(QualityCheck))}
    item_rows = "".join(
        f"<tr><td>{escape(checks[it.check_id].code if it.check_id in checks else '?')}</td>"
        f"<td>{escape(checks[it.check_id].name if it.check_id in checks else '')}</td>"
        f"<td>{escape(it.result)}</td><td>{escape(it.value)}</td>"
        f"<td>{escape(it.notes)}</td></tr>" for it in items)
    photos = db.scalars(select(QualityPhoto)
                        .where(QualityPhoto.inspection_id == i.id)
                        .order_by(QualityPhoto.id)).all()
    photo_rows = "".join(f"<li>{escape(p.filename.split('/')[-1])} "
                         f"({p.size_bytes} bytes)</li>" for p in photos)
    body = f"""
<div class='grid'>
 <div><b>Inspection:</b> {escape(i.inspection_no)}</div><div><b>Status:</b> {i.status}</div>
 <div><b>Lot:</b> {escape(lot.lot_no) if lot else '—'}</div>
 <div><b>Owner:</b> {_party(db, lot.owner_id) if lot else '—'}</div>
 <div><b>Inspector:</b> user #{i.inspector_id}</div><div><b>Verdict:</b> {i.verdict}</div>
</div>
<table><tr><th class='right'>Sampled units</th><th class='right'>Accepted kg</th>
<th class='right'>Rejected kg</th><th class='right'>Downgraded kg</th>
<th>Target grade</th></tr>
<tr><td class='right'>{i.sampled_units}</td><td class='right'>{float(i.accepted_kg):,.3f}</td>
<td class='right'>{float(i.rejected_kg):,.3f}</td>
<td class='right'>{float(i.downgraded_kg):,.3f}</td>
<td>{db.get(Grade, i.target_grade_id).name if i.target_grade_id else '—'}</td></tr></table>
<h3>Check results</h3>
<table><tr><th>Code</th><th>Check</th><th>Result</th><th>Value</th><th>Notes</th></tr>
{item_rows or '<tr><td colspan=5>no item rows</td></tr>'}</table>
<div class='box'><b>Notes:</b> {escape(i.notes) or '—'}<br>
<b>Photos:</b> {photo_rows or '—'}</div>
<div class='box'><b>Applied:</b> {i.applied_at or 'not applied'} ·
result: {escape(str(i.result_meta))}</div>
<div class='grid' style='margin-top:24px'><div>Inspector: ____________</div>
<div style='text-align:right'>Manager: ____________</div></div>"""
    return _page(f"Quality inspection {i.inspection_no}", body)


def order_html(db: Session, o) -> str:
    owner = db.get(Party, o.owner_id)
    lines = db.scalars(select(OutwardOrderLine)
                       .where(OutwardOrderLine.order_id == o.id)
                       .order_by(OutwardOrderLine.line_no)).all()
    vmap = {v.id: v.name for v in db.scalars(select(Variety))}
    gmap = {g.id: g.name for g in db.scalars(select(Grade))}
    line_rows = "".join(
        f"<tr><td>{l.line_no}</td>"
        f"<td>{escape(vmap.get(l.variety_id, '—') if l.variety_id else '—')}</td>"
        f"<td>{escape(gmap.get(l.grade_id, '—') if l.grade_id else '—')}</td>"
        f"<td class='right'>{float(l.qty_kg):,.3f}</td><td class='right'>{l.units}</td>"
        f"<td>{escape(l.note)}</td></tr>" for l in lines)
    body = f"""
<div class='grid'>
 <div><b>Order:</b> {escape(o.order_no)}</div><div><b>Status:</b> {o.status}</div>
 <div><b>Owner:</b> {_party(db, o.owner_id)}</div>
 <div><b>Destination:</b> {escape(o.destination) or '—'}</div>
 <div><b>Created:</b> {str(o.created_at)[:16]}</div>
 <div><b>Confirmed:</b> {str(o.confirmed_at)[:16] if o.confirmed_at else '—'}</div>
</div>
<table><tr><th>#</th><th>Variety</th><th>Grade</th><th class='right'>Qty kg</th>
<th class='right'>Units</th><th>Note</th></tr>{line_rows}</table>
<div class='box'><b>Notes:</b> {escape(o.notes) or '—'}<br>
<b>Closed:</b> {escape(o.closed_reason) or '—'}</div>
<div class='grid' style='margin-top:24px'><div>Order by: ____________</div>
<div style='text-align:right'>Warehouse: ____________</div></div>"""
    return _page(f"Outward order {o.order_no}", body)


def delivery_html(db: Session, d) -> str:
    dn = db.get(DispatchNote, d.dispatch_id)
    owner = db.get(Party, d.owner_id)
    body = f"""
<div class='grid'>
 <div><b>Delivery:</b> {escape(d.delivery_no)}</div><div><b>Status:</b> {d.status}</div>
 <div><b>Dispatch:</b> {escape(dn.dispatch_no) if dn else '—'}</div>
 <div><b>Owner:</b> {_party(db, d.owner_id)}</div>
 <div><b>Vehicle:</b> {escape(d.vehicle_no)}</div><div><b>Driver:</b> {escape(d.driver_name)}</div>
 <div><b>Departed:</b> {d.departed_at:%Y-%m-%d %H:%M}</div>
 <div><b>Confirmed:</b> {d.confirmed_at:%Y-%m-%d %H:%M if d.confirmed_at else '—'}</div>
</div>
<table><tr><th class='right'>Planned kg</th><th class='right'>Planned units</th>
<th class='right'>Delivered kg</th><th class='right'>Delivered units</th>
<th class='right'>Shortage kg</th><th class='right'>Damage kg</th></tr>
<tr><td class='right'>{float(d.planned_kg):,.3f}</td><td class='right'>{d.planned_units}</td>
<td class='right'>{float(d.delivered_kg):,.3f}</td><td class='right'>{d.delivered_units}</td>
<td class='right'>{float(d.shortage_kg):,.3f}</td>
<td class='right'>{float(d.damage_kg):,.3f}</td></tr></table>
<div class='box'><b>Received by:</b> {escape(d.received_by_name) or '—'} ·
{escape(d.received_by_phone) or '—'}<br>
<b>Shortage/damage note:</b> {escape(d.shortage_note) or '—'}<br>
<b>Notes:</b> {escape(d.notes) or '—'}</div>
<div class='grid' style='margin-top:24px'><div>Delivered by: ____________</div>
<div style='text-align:right'>Received by: ____________</div></div>"""
    return _page(f"Delivery {d.delivery_no}", body)
