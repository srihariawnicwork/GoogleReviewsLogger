// Parse Groq output and build the full Excel row + reply draft.
// toExcelDateTime() accepts "DD/MM/YYYY[ HH:MM[:SS]]" (what the scraper sends)
// OR an ISO string, and emits locale-safe "YYYY-MM-DD HH:MM:SS" for Excel.
function toExcelDateTime(val) {
  if (!val) return '';
  const s = String(val).replace(/^'/, '').trim();
  const m = s.match(/^(\d{2})\/(\d{2})\/(\d{4})(?:\s+(\d{2}):(\d{2})(?::(\d{2}))?)?$/);
  if (m) {
    const dd = m[1], mm = m[2], yyyy = m[3], hh = m[4] || '00', mi = m[5] || '00', ss = m[6] || '00';
    return `${yyyy}-${mm}-${dd} ${hh}:${mi}:${ss}`;
  }
  const d = new Date(s);
  if (isNaN(d.getTime())) return s;
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

const groqResponse = $input.item.json?.choices?.[0]?.message?.content ?? '';
let llm;
try {
  llm = JSON.parse(groqResponse);
} catch(e) {
  llm = { sentiment:'NEGATIVE', agent_name:'N/A', complaint_category:'Other',
          details_provided:false, reason_for_review:'parse error', review_status:'Negative' };
}

const original = $('New Review Received').first().json.body ?? $('New Review Received').first().json;
const customerName = original['Customer Name in Google'] || 'Valued Customer';

// Positive reply rotation by day of month
const POSITIVE_REPLIES = [
  "Thank you for your kind feedback. We're delighted to know you had a positive experience with us. Your satisfaction is our top priority, and we truly appreciate your support.",
  "We truly appreciate your wonderful review. Your satisfaction is our top priority, and we're pleased to hear that we met your expectations.",
  "Thank you for your positive feedback. Your satisfaction is our top priority, and we're glad we could provide you with a great experience."
];
const day = parseInt(original.Day || '1');
let posIdx; if (day <= 10) posIdx = 0; else if (day <= 20) posIdx = 1; else posIdx = 2;

// Negative reply drafts (plain text; no markdown links)
const SCENARIO_1 = `Dear ${customerName},\n\nThank you for taking the time to share your feedback. To help us look into this and support you better, could you please share your claim number and contact details with us?\n\nYou can reach us directly on:\nPhone: +971 600 544 040\nEmail: customercare@awnic.com\nWebsite: www.awnic.com\n\nOur team will take this up on priority and ensure it is reviewed with the attention it deserves. Thank you!`;
const SCENARIO_2 = `Dear ${customerName},\n\nThank you for speaking with us and sharing your experience. We truly regret that your recent experience did not meet the standards we aim to deliver. Your feedback has been taken seriously and shared with the relevant team for immediate review.\n\nPlease be assured that your case is being closely followed, and we are working to address the concerns raised and resolve this at the earliest.\n\nWe appreciate your patience and the opportunity to make this right. Thank you!`;

let selectedReply;
if (llm.sentiment === 'POSITIVE') selectedReply = POSITIVE_REPLIES[posIdx];
else selectedReply = llm.details_provided === true ? SCENARIO_2 : SCENARIO_1;

return {
  json: {
    review_id: original.review_id,
    'Review Date': toExcelDateTime(original['Review Date']),
    Rating: original.Rating,
    Review: original.Review,
    'Customer Name in Google': customerName,
    Branch: original.Branch,
    Day: original.Day,
    Month: original.Month,
    Year: original.Year,

    'Reason for Review (AI Summarized)': llm.complaint_category || 'N/A',
    'Agent Name': (llm.agent_name && llm.agent_name !== 'N/A') ? llm.agent_name : '',
    'Review Status': llm.review_status || '',

    sentiment: llm.sentiment,
    complaint_category: llm.complaint_category || 'Other',
    details_provided: llm.details_provided === true,

    selected_reply: selectedReply,
    'Suggested Reply': selectedReply,
    'Response Status': (original.owner_responded ? 'Posted Reply' : 'Pending Reply'),

    'Latest Action Taken To Customer': '',
    'Latest Action Date - Customer': '',
    'Latest Action Date - Internal': '',
    'Complaint Resolution Days': '',

    action: original.action,
    previous_rating: original.previous_rating,
    'Logged At': toExcelDateTime(original['Logged At']),
    'Customer Last Update': toExcelDateTime(original['Customer Last Update']),
    'Owner Reply': original['Owner Reply'] || '',
    'Agent Reply Time': toExcelDateTime(original['Agent Reply Time']),
    'Review Link': original['Review Link'] || ''
  }
};