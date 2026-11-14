"""Export an agent-placed order as Peppol BIS Ordering 3 documents (UBL 2.1).

The identifiers and address below are explicitly synthetic, but they have the same
shape as production Peppol data: endpoint IDs are valid GLNs and the seller has a
country-qualified postal address.  The test suite runs the output through the OASIS
UBL 2.1 schema and the published OpenPeppol T01 Schematron.
"""
from xml.sax.saxutils import escape as x

NS = 'xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"'


def _amt(cents) -> str:
    return f"{cents / 100:.2f}"


BUYER_GLN = "1234567890128"
SELLER_GLN = "1234567890135"
# The challenge API uses the short UNECE names for some package units.  Peppol's
# Recommendation 20 list uses the corresponding X-prefixed codes for these.
# The last five are sales units the gateway has no name for and passes on as the challenge data writes them
# (canister, pallet, bundle, display pack, dozen).
PEPPOL_UNITS = {"BO": "XBO", "BG": "XBG", "CS": "XCS", "PK": "XPK", "SA": "XSA", "BX": "XBX", "JR": "XJR", "CT": "XCT", "RO": "XRO",
                "KAN": "XCI", "PAL": "XPX", "NIP": "XBE", "DSP": "XPK", "DZ": "DZN"}


def _party(tag, name, endpoint, address=True) -> str:
    postal = ("<cac:PostalAddress><cac:Country><cbc:IdentificationCode>FI</cbc:IdentificationCode>"
              "</cac:Country></cac:PostalAddress>" if address else "")
    return (f"<cac:{tag}><cac:Party><cbc:EndpointID schemeID=\"0088\">{x(endpoint)}</cbc:EndpointID>"
            f"{postal}<cac:PartyLegalEntity><cbc:RegistrationName>{x(name)}</cbc:RegistrationName></cac:PartyLegalEntity></cac:Party></cac:{tag}>")


def _item(l) -> str:
    w = l["wholesale"]
    gtin = f"<cac:StandardItemIdentification><cbc:ID schemeID=\"0160\">{x(w['gtin'])}</cbc:ID></cac:StandardItemIdentification>" if w.get("gtin") else ""
    return f"<cac:Item><cbc:Name>{x(l['item']['title'])}</cbc:Name><cac:SellersItemIdentification><cbc:ID>{x(l['item']['id'])}</cbc:ID></cac:SellersItemIdentification>{gtin}</cac:Item>"


def _qty(l) -> str:
    unit = l["item"]["quantity_unit"]["unit"]
    return f"<cbc:Quantity unitCode=\"{x(PEPPOL_UNITS.get(unit, unit))}\">{l['wholesale']['units']:g}</cbc:Quantity>"


def _unit(l) -> str:
    unit = l["item"]["quantity_unit"]["unit"]
    return PEPPOL_UNITS.get(unit, unit)


def _head(o, kind):
    w = o["wholesale"]
    return (f"<cbc:CustomizationID>urn:fdc:peppol.eu:poacc:trns:{kind}:3</cbc:CustomizationID><cbc:ProfileID>urn:fdc:peppol.eu:poacc:bis:ordering:3</cbc:ProfileID>"
            f"<cbc:ID>{x(w.get('purchase_order') or o['label'])}</cbc:ID><cbc:IssueDate>{w['placed_at'][:10]}</cbc:IssueDate><cbc:IssueTime>{w['placed_at'][11:19]}</cbc:IssueTime>")


def order_xml(o) -> str:
    w = o["wholesale"]
    sub = next(t["amount"] for t in o["totals"] if t["type"] == "subtotal")
    tot = next(t["amount"] for t in o["totals"] if t["type"] == "total")
    lines = "".join(
        f"<cac:OrderLine><cac:LineItem><cbc:ID>{n + 1}</cbc:ID>{_qty(l)}<cbc:LineExtensionAmount currencyID=\"EUR\">{_amt(l['totals'][0]['amount'])}</cbc:LineExtensionAmount>"
        f"<cac:Price><cbc:PriceAmount currencyID=\"EUR\">{_amt(l['item']['price'])}</cbc:PriceAmount></cac:Price>{_item(l)}</cac:LineItem></cac:OrderLine>"
        for n, l in enumerate(o["line_items"])
    )
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<Order xmlns="urn:oasis:names:specification:ubl:schema:xsd:Order-2" {NS}>{_head(o, "order")}'
            f"<cbc:Note>Placed by AI agent {x(w['placed_by'])} under mandate {x(w['evidence']['mandate_id'])}. Offer digest sha-256:{w['evidence']['offer_digest']}</cbc:Note>"
            f"<cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode><cac:ValidityPeriod><cbc:EndDate>{w['placed_at'][:10]}</cbc:EndDate></cac:ValidityPeriod>"
            f"{_party('BuyerCustomerParty', o['wholesale']['buyer']['name'] or 'Buyer', BUYER_GLN, address=False)}{_party('SellerSupplierParty', 'Valio Aimo (mock)', SELLER_GLN)}"
            f"<cac:Delivery><cac:RequestedDeliveryPeriod><cbc:StartDate>{w['delivery_date']}</cbc:StartDate><cbc:EndDate>{w['delivery_date']}</cbc:EndDate></cac:RequestedDeliveryPeriod></cac:Delivery>"
            f"<cac:AnticipatedMonetaryTotal><cbc:LineExtensionAmount currencyID=\"EUR\">{_amt(sub)}</cbc:LineExtensionAmount><cbc:PayableAmount currencyID=\"EUR\">{_amt(tot)}</cbc:PayableAmount></cac:AnticipatedMonetaryTotal>"
            f"{lines}</Order>\n")


def order_response_xml(o) -> str:
    """AP = accepted. A substituted line is reported with LineStatusCode 3 and cac:SellerSubstitutedLineItem."""
    w = o["wholesale"]
    changed = any(l["wholesale"].get("substitutes_for") for l in o["line_items"])
    # T76 says an AP response carries no lines.  CA responses carry the changed
    # lines so the buyer can match them to the original order.
    lines = ""
    for n, l in enumerate(o["line_items"]):
        s = l["wholesale"].get("substitutes_for")
        if s:
            orig = f"<cac:Item><cbc:Name>{x(s['for_title'])}</cbc:Name><cac:SellersItemIdentification><cbc:ID>{x(s['for'])}</cbc:ID></cac:SellersItemIdentification></cac:Item>"
            lines += (f"<cac:OrderLine><cac:LineItem><cbc:ID>{n + 1}</cbc:ID><cbc:LineStatusCode>3</cbc:LineStatusCode>{_qty(l)}{orig}</cac:LineItem>"
                      f"<cac:SellerSubstitutedLineItem><cbc:ID>{n + 1}</cbc:ID>{_item(l)}</cac:SellerSubstitutedLineItem>"
                      f"<cac:OrderLineReference><cbc:LineID>{n + 1}</cbc:LineID></cac:OrderLineReference></cac:OrderLine>")
        elif changed:
            lines += (f"<cac:OrderLine><cac:LineItem><cbc:ID>{n + 1}</cbc:ID><cbc:LineStatusCode>5</cbc:LineStatusCode>{_qty(l)}{_item(l)}</cac:LineItem>"
                      f"<cac:OrderLineReference><cbc:LineID>{n + 1}</cbc:LineID></cac:OrderLineReference></cac:OrderLine>")
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<OrderResponse xmlns="urn:oasis:names:specification:ubl:schema:xsd:OrderResponse-2" {NS}>{_head(o, "order_response")}'
            f"<cbc:OrderResponseCode>{'CA' if changed else 'AP'}</cbc:OrderResponseCode><cbc:Note>Valio Aimo order {x(o['label'])}. Signed confirmation: {x(o['permalink_url'])}</cbc:Note>"
            f"<cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode><cac:OrderReference><cbc:ID>{x(w.get('purchase_order') or o['label'])}</cbc:ID></cac:OrderReference>"
            f"{_party('SellerSupplierParty', 'Valio Aimo (mock)', SELLER_GLN, address=False)}{_party('BuyerCustomerParty', o['wholesale']['buyer']['name'] or 'Buyer', BUYER_GLN, address=False)}"
            f"<cac:Delivery><cac:PromisedDeliveryPeriod><cbc:StartDate>{w['delivery_date']}</cbc:StartDate><cbc:EndDate>{w['delivery_date']}</cbc:EndDate></cac:PromisedDeliveryPeriod></cac:Delivery>"
            f"{lines}</OrderResponse>\n")


def amended_response_xml(o, st) -> str:
    """The order as it stands after its signed amendments (short pick, delay, recall), as a second OrderResponse.
    CA = changed, RE = cancelled. Line status 5 = as ordered, 3 = quantity changed, 7 = not delivered."""
    w = o["wholesale"]
    by_id = {l["id"]: l for l in st["lines"]}
    lines = ""
    for n, l in enumerate(o["line_items"]):
        s = by_id[l["id"]]
        code = 5 if s["units"] == s["ordered"] else 3 if s["units"] > 0 else 7
        lines += (f"<cac:OrderLine><cac:LineItem><cbc:ID>{n + 1}</cbc:ID><cbc:Note>{x(s['status'])}, lot {x(s['lot'])}</cbc:Note><cbc:LineStatusCode>{code}</cbc:LineStatusCode>"
                  f"<cbc:Quantity unitCode=\"{x(_unit(l))}\">{s['units']:g}</cbc:Quantity>{_item(l)}</cac:LineItem>"
                  f"<cac:OrderLineReference><cbc:LineID>{n + 1}</cbc:LineID></cac:OrderLineReference></cac:OrderLine>")
    note = f"Valio Aimo order {o['label']} as it stands after {len(st['amendments'])} signed amendment(s): {', '.join(st['amendments']) or 'none'}. Credited {_amt(st['credit_ex_vat'])} EUR ex VAT."
    if st["linked_orders"]:
        note += " Replacement goods are on linked order(s) " + ", ".join(k["label"] for k in st["linked_orders"]) + "."
    code = "RE" if st["status"] == "cancelled" else "CA" if st["amendments"] else "AP"
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<OrderResponse xmlns="urn:oasis:names:specification:ubl:schema:xsd:OrderResponse-2" {NS}>{_head(o, "order_response")}'
            f"<cbc:OrderResponseCode>{code}</cbc:OrderResponseCode><cbc:Note>{x(note)} Signed confirmation and amendments: {x(o['permalink_url'])}</cbc:Note>"
            f"<cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode><cac:OrderReference><cbc:ID>{x(w.get('purchase_order') or o['label'])}</cbc:ID></cac:OrderReference>"
            f"{_party('SellerSupplierParty', 'Valio Aimo (mock)', SELLER_GLN, address=False)}{_party('BuyerCustomerParty', o['wholesale']['buyer']['name'] or 'Buyer', BUYER_GLN, address=False)}"
            f"<cac:Delivery><cac:PromisedDeliveryPeriod><cbc:StartDate>{st['delivery_date']}</cbc:StartDate><cbc:EndDate>{st['delivery_date']}</cbc:EndDate></cac:PromisedDeliveryPeriod></cac:Delivery>"
            f"{lines}</OrderResponse>\n")
