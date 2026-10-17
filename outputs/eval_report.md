# Evaluation report

Generated: 2026-10-16T18:11:36+05:30

| # | Status | Check | Group | Detail |
| --- | --- | --- | --- | --- |
| 1 | PASS | data.file_exists | Data | data/reviews.csv: 2358 rows, 7 columns |
| 2 | PASS | data.required_columns | Data | required columns present: text, date, rating |
| 3 | PASS | data.no_private_columns | Data | 7 columns, no name/user/email column |
| 4 | PASS | data.date_span_weeks | Data | 2026-07-21 to 2026-10-11 = 11.7 weeks |
| 5 | PASS | insights.theme_counts | Insights | 5 themes, exactly 3 top_themes |
| 6 | PASS | insights.problem_counts | Insights | 5 problem_count values match outputs/review_themes.csv |
| 7 | PASS | insights.quotes_exact | Insights | 3 quotes found verbatim in data/reviews.csv |
| 8 | PASS | insights.fee_issue | Insights | stance_counts 4 keys, pattern_counts 3 keys |
| 9 | PASS | pulse.word_count | Pulse | 247/250 words |
| 10 | PASS | pulse.sections | Pulse | all 4 sections present in weekly_pulse.md |
| 11 | PASS | pulse.action_ideas | Pulse | exactly 3 action ideas in pulse.json and the markdown |
| 12 | PASS | pulse.excerpts_exact | Pulse | 3 excerpts are exact substrings of their reviews |
| 13 | PASS | pulse.email_summary | Pulse | 7 lines, <= 12 words, no digits/banned words |
| 14 | PASS | fee.bullets_and_review | Fee explainer | 5 bullets, needs_human_review is false |
| 15 | PASS | fee.source_links | Fee explainer | 2 source_links, both on official domains |
| 16 | PASS | fee.last_checked | Fee explainer | last_checked = 2026-10-14 |
| 17 | PASS | fee.numbers_in_sources | Fee explainer | 4 fact bullets, every number found in its cited S*.txt |
| 18 | PASS | fee.fact_check_summary | Fee explainer | fact_check: not_supported = 0, unverified = 0 |
| 19 | PASS | fee.no_banned_words | Fee explainer | no banned words in 5 bullets |
| 20 | PASS | approval.record_fields | Approval/MCP | latest record outputs/approvals/11e3efc9-98e8-4d42-948b-00738efa6e71.json has approval_id/approved_at/payload_sha256 |
| 21 | PASS | approval.payload_hash | Approval/MCP | payload_sha256 recomputes to 00e318ad6abf (publish.compute_hash) |
| 22 | PASS | publish.log_steps_ok | Approval/MCP | latest log line: doc append OK, read-back verified, email draft OK |
| 23 | PASS | publish.email_subject | Approval/MCP | subject starts with 'Weekly Product Pulse + Customer Clarification —' |
| 24 | PASS | publish.email_body_footer | Approval/MCP | 'Not sent automatically' present in rendered email body |
| 25 | PASS | mcp.no_send_tools | Approval/MCP | no 'send' tool in REQUIRED_TOOLS (append_to_doc, create_email_draft, read_doc) |
| 26 | PASS | edge.empty_file | Edge cases | empty file rejected: \empty.csv is empty (0 bytes) |
| 27 | PASS | edge.missing_text_column | Edge cases | missing text column rejected: missing required column(s): text |
| 28 | PASS | edge.too_few_rows | Edge cases | 20 rows rejected: only 20 reviews remain after cleaning (minimum 100) |
| 29 | PASS | edge.bad_ratings_dropped | Edge cases | 3 bad ratings (0, 7, 'good') dropped with a warning, 120 kept |
| 30 | PASS | edge.pii_masked | Edge cases | email masked as [email] and phone number as [phone] |
| 31 | PASS | edge.private_column_dropped | Edge cases | user_name column dropped with a warning |
| 32 | PASS | edge.twenty_weeks_trimmed | Edge cases | 20 weeks of reviews trimmed to the latest 12 weeks |

**32/32 checks passed**
