# Screenshots

[中文](README.md) · [Back to the project](../README.en.md)

Screenshots of LocSwap's main screens and five example conversations with the seller advisor. The UI is in Chinese; the captions below explain what each screen shows. All items, buyers and sales in the images are demo data, and the demand and sales history comes from the 12 weeks of activity simulated by `scripts/simulate_platform.py`. Captured on 2026-10-12.

The same screenshots are also collected in one PDF: [调试素材.pdf](调试素材.pdf).

---

## Buyer side

### Home

![Home](01-home.png)

- The top bar holds the main navigation: Home, Search, Cart, Sell and Messages. The current location sits at the top right, and distances are measured from it.
- The globe on the right is drawn with three.js.
- "Latest listings" can be filtered by keyword, category, subcategory, price range, distance and condition, with 5 sort orders. Every item card has a Reserve button.

### Map search

![Map search](02-map-search.jpg)

- Conversational search is on the left. Describe what you want in plain words, e.g. "a used bike under 150, close to me". The system parses price, distance, category and other conditions out of the sentence before searching; see [Retrieval pipeline](../README.en.md#retrieval-pipeline) in the main README.
- The map in the middle uses Leaflet with OpenStreetMap tiles. The code ships with 18 apartments and townhouses around the JHU Homewood campus as meeting points. Each one gets a pin, and the number on a pin is how many items are listed there. Opening a location lists its items, which can be reserved directly.
- Category filters are on the right, each with its item count.

---

## Seller side: the sales advisor

### Entry screen

![Sales advisor](03-seller-advisor.png)

- The "sale-plan monitoring" pill at the top is a master switch. Next to it is how many sale plans exist and how many are being watched right now. The switch writes the same field as the `set_plan_watch` tool in the conversation, so telling the advisor "turn all reminders off" also turns the pill off.
- The suggested prompts in the middle cover what the advisor can do: find potential buyers, check which items have the most interested buyers, check pricing, and list an item from a photo.

All five examples below are about the same item, a "white desk lamp". Every message first goes through the intent router (call site D) and is then handed to the matching agent. When the seller only says "lamp", the system compares the head noun of that phrase with the head nouns stored on the seller's listed items and resolves it to the white desk lamp. If that comparison is inconclusive, a model decides instead (call site I).

### Example 1 · Demand for one item

> Can you check the demand for my lamp?

![Example 1](04-task1-demand.png)

The sales advisor (call site C) calls the `analyze_demand` tool. The card shows three layers:

- **This item**: 0 people are looking for it. A search only counts if its wording overlaps with the item name.
- **Its category**: 28 active buyers and 31 searches in Home & Kitchen.
- **The listing's own traffic**: 34 impressions and 2 opens.

"How buyers search" lists only phrasings used by at least 3 people. Phrasings used by fewer than 3 people are hidden, so no individual buyer can be singled out.

> **Reading note**: demand data currently comes only from the simulated history (see "Known limitations" in the main README). That history ends on 2026-09-14, so only 3 days fall inside the 30-day window. The card says "insufficient data" and doesn't call a trend.

### Example 2 · "Help me sell the lamp"

> Can you help me sell my white desk lamp?

![Example 2: summary](05-task2-overview.png)

This is a full check. In a single turn the advisor calls three read-only tools, gives its conclusion, and then attaches three evidence cards.

- **Potential buyers** (`find_potential_buyers`, above): within 50 km and at a match threshold of 0.53, no public want qualified. The card says this means "searched, nothing qualified" rather than a failed query, and suggests three next steps: widen the distance, look at demand for the whole category, or fill in the item's details.
- **Sold prices** (`analyze_sold_prices`, first image below): the min, P25, median, P75 and max of comparable sales, with the seller's current price marked on the distribution, plus three reference prices: sell fast, balanced, and test high.
- **Demand** (`analyze_demand`, second image below): the same card as in Example 1.

![Example 2: sold-price evidence](06-task2-sold-prices.png)

![Example 2: demand](07-task2-demand.png)

> **Reading note**: sold-price samples are drawn **by category**, which is why the samples shown are cookware, storage boxes and water bottles rather than lamps. "High confidence" only means there are at least 8 samples; it doesn't mean they're comparable to a lamp. The advisor only makes suggestions and never changes the seller's price.

### Example 3 · List an item from a photo

> I want to list this lamp (with a photo)

![Example 3](08-task3-publish-draft.png)

This message goes through the publishing flow:

1. A compliance review of the text and the photo against the rule set (call site E).
2. An editable listing draft extracted from the photo and text (call site F): title, summary, description, category, subcategory, condition and color.
3. The bottom of the card lists the fields that still need confirming, here brand, condition and size.

The price field is marked "you decide"; the draft never sets a price for the seller. When the seller clicks "Confirm and publish", the server reviews the submitted content once more and only lists the item if it passes.

### Example 4 · Sell later

> I want to get rid of my white desk lamp in graduation season

![Example 4](09-task4-sale-plan.png)

The message is first recognised as "wants to sell, but not now" (call site H) and then handed to the sale-plan agent (call site G):

- "Graduation season" is resolved to 2027-05-15. The card marks this as an inferred date and asks the seller to confirm it.
- The item is hidden from search. It stays in the seller's inventory, but buyers can't find it for now.
- Starting 6 weeks before the sell date (2027-04-03), the category is checked every Monday:
  - more than 10 reservations in a week sends an in-app notice and an email;
  - searches up 30% or more week over week sends an email only.
- The system only sends reminders; it never relists the item on the seller's behalf.
- The plan can be paused or cancelled from the card. If the seller relists the item early from "My listings", the plan is cancelled as well.

### Example 5 · Find buyers nearby

> Who nearby is looking for a lamp?

![Example 5](10-task5-find-buyers.png)

This message only calls `find_potential_buyers`, with the same conditions as in Example 2. When nothing qualifies, the card states the query conditions and suggests next steps.

When a want does qualify, the buyer appears as an anonymous card ("Candidate N"). Pushing the item to a buyer is proposed by the advisor first and only happens after the seller confirms. Pushes are also rate-limited; see [Privacy and security](../README.en.md#privacy-and-security) in the main README.
