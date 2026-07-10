// Post Open Code Review JSON findings to a GitHub PR.
// Adapted from alibaba/open-code-review examples/github_actions/ocr-review.yml

const fs = require('fs');
const crypto = require('crypto');

module.exports = async function postReviewComments({ github, context, core }) {
  const path = core.getInput('ocr_result_path') || process.env.OCR_RESULT_PATH || '/tmp/ocr-result.json';
  const stderrPath = core.getInput('ocr_stderr_path') || process.env.OCR_STDERR_PATH || '/tmp/ocr-stderr.log';

  const runId = Number.isFinite(context.runId) ? context.runId : 0;
  const runAttempt = Number.isFinite(context.runAttempt) ? context.runAttempt : 1;
  const RUN_TAG = `${runId}-${runAttempt}`;
  const REVIEW_TAG = `<!-- ocr-review-${RUN_TAG} -->`;
  const SUMMARY_TAG = `<!-- ocr-summary-${RUN_TAG} -->`;

  let result;
  try {
    const raw = fs.readFileSync(path, 'utf8');
    result = JSON.parse(raw);
  } catch (e) {
    core.info(`Failed to parse OCR output: ${e.message}`);
    if (fs.existsSync(stderrPath)) {
      const stderr = fs.readFileSync(stderrPath, 'utf8').trim();
      if (stderr) {
        await github.rest.issues.createComment({
          owner: context.repo.owner,
          repo: context.repo.repo,
          issue_number: context.issue.number,
          body: formatErrorComment(stderr),
        });
      }
    }
    return;
  }

  const comments = result.comments || [];
  const warnings = result.warnings || [];

  if (comments.length === 0) {
    const message = result.message || 'No comments generated. Looks good to me.';
    const stderr = fs.existsSync(stderrPath)
      ? fs.readFileSync(stderrPath, 'utf8').trim()
      : '';
    // OCR often says "check your LLM configuration and API key" for any
    // all-files-failed case (including HTTP timeouts). Prefer a clearer note.
    if (looksLikeOperationalFailure(message, stderr, result)) {
      await github.rest.issues.createComment({
        owner: context.repo.owner,
        repo: context.repo.repo,
        issue_number: context.issue.number,
        body: formatErrorComment(stderr || message, message),
      });
      return;
    }
    await github.rest.issues.createComment({
      owner: context.repo.owner,
      repo: context.repo.repo,
      issue_number: context.issue.number,
      body: `✅ **OpenCodeReview**: ${message}`,
    });
    return;
  }

  const prNumber = context.issue.number;
  let commitSha;
  if (context.eventName === 'pull_request_target') {
    commitSha = context.payload.pull_request.head.sha;
  } else {
    const { data: pullRequest } = await github.rest.pulls.get({
      owner: context.repo.owner,
      repo: context.repo.repo,
      pull_number: prNumber,
    });
    commitSha = pullRequest.head.sha;
  }

  const reviewComments = [];
  const commentsWithoutLine = [];

  for (const comment of comments) {
    const hasValidLine = (comment.start_line >= 1) || (comment.end_line >= 1);
    if (!hasValidLine) {
      commentsWithoutLine.push({ comment });
      continue;
    }
    reviewComments.push({
      comment,
      id: newCommentId(),
      lines: resolveLines(comment),
    });
  }

  const totalCount = comments.length;
  const inlineCount = reviewComments.length;
  const summaryCount = commentsWithoutLine.length;
  let summaryBody = buildSummaryBody(totalCount, inlineCount, summaryCount, warnings);
  summaryBody += formatSummaryComments(commentsWithoutLine);
  summaryBody = REVIEW_TAG + '\n' + summaryBody;

  let successCount = 0;
  let failedCount = 0;
  const failedComments = [];

  function parseNonNegInt(val, defaultVal) {
    const n = parseInt(val, 10);
    return Number.isFinite(n) && n >= 0 ? n : defaultVal;
  }

  const MAX_RETRIES = parseNonNegInt(process.env.OCR_MAX_RETRIES, 3);
  const SUCCESS_DELAY = parseNonNegInt(process.env.OCR_SUCCESS_DELAY, 2000);
  const FAILURE_DELAY = parseNonNegInt(process.env.OCR_FAILURE_DELAY, 1000);
  const LOW_REMAINING_THRESHOLD = parseNonNegInt(process.env.OCR_LOW_REMAINING_THRESHOLD, 3);
  const LOW_REMAINING_SPACING = parseNonNegInt(process.env.OCR_LOW_REMAINING_SPACING, 10000);
  const READ_SUCCESS_DELAY = parseNonNegInt(process.env.OCR_READ_SUCCESS_DELAY, 500);
  const READ_LOW_REMAINING_SPACING = parseNonNegInt(process.env.OCR_READ_LOW_REMAINING_SPACING, 5000);

  try {
    const batchRes = await github.rest.pulls.createReview({
      owner: context.repo.owner,
      repo: context.repo.repo,
      pull_number: prNumber,
      commit_id: commitSha,
      body: summaryBody,
      event: 'COMMENT',
      comments: reviewComments.map(toReviewPayload),
    });
    successCount = reviewComments.length;
    core.info(`Posted review with ${successCount} inline comments`);
    logRateLimitQuota(batchRes, 'after batch createReview');
  } catch (e) {
    core.info(`Batch createReview failed: ${e.message}`);
    let toRetry = reviewComments;
    for (const item of toRetry) {
      const { comment, id } = item;
      let posted = false;
      for (let attempt = 0; attempt <= MAX_RETRIES && !posted; attempt++) {
        try {
          await github.rest.pulls.createReview({
            owner: context.repo.owner,
            repo: context.repo.repo,
            pull_number: prNumber,
            commit_id: commitSha,
            body: '',
            event: 'COMMENT',
            comments: [toReviewPayload(item)],
          });
          successCount++;
          posted = true;
          await sleep(SUCCESS_DELAY);
        } catch (innerE) {
          if (attempt >= MAX_RETRIES) {
            failedCount++;
            failedComments.push({ comment, error: innerE.message });
            await sleep(FAILURE_DELAY);
          } else {
            await sleep(2000 * (attempt + 1));
          }
        }
      }
    }
  }

  let finalBody = buildSummaryBody(totalCount, successCount, commentsWithoutLine.length + failedComments.length, warnings);
  finalBody += formatSummaryComments(commentsWithoutLine);
  finalBody += `\n\n---\n\n📊 **Posting Statistics:**\n- ✅ Successfully posted: ${successCount} comment(s)`;
  if (failedCount > 0) {
    finalBody += `\n- ❌ Failed to post: ${failedCount} comment(s)`;
  }
  if (failedComments.length > 0) {
    finalBody += '\n\n---\n\n### ⚠️ Inline comments shown in summary';
    for (const { comment, error } of failedComments) {
      finalBody += '\n\n---\n\n' + formatCommentMarkdown(comment, error);
    }
  }
  finalBody = SUMMARY_TAG + '\n' + finalBody;

  await github.rest.issues.createComment({
    owner: context.repo.owner,
    repo: context.repo.repo,
    issue_number: prNumber,
    body: finalBody,
  });

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function newCommentId() {
    return `ocr-${RUN_TAG}-${crypto.randomBytes(8).toString('hex')}`;
  }

  function resolveLines(comment) {
    const start = comment.start_line;
    const end = comment.end_line;
    if (start >= 1 && end >= 1 && start !== end) {
      return { start_line: start, line: end, start_side: 'RIGHT', side: 'RIGHT' };
    } else if (end >= 1) {
      return { line: end, side: 'RIGHT' };
    } else if (start >= 1) {
      return { line: start, side: 'RIGHT' };
    }
    return {};
  }

  function toReviewPayload(item) {
    return {
      path: item.comment.path,
      body: buildBody(item.comment, item.id),
      ...item.lines,
    };
  }

  function buildBody(comment, id) {
    let body = `<!-- ${id} -->\n`;
    body += comment.content || '';
    if (comment.suggestion_code && comment.existing_code) {
      body += '\n\n**Suggestion:**\n';
      body += fencedBlock(comment.suggestion_code, 'suggestion');
    }
    return body;
  }

  function formatCommentMarkdown(comment, error) {
    let md = `### 📄 \`${comment.path}\``;
    if (comment.start_line && comment.end_line) {
      md += ` (L${comment.start_line}-L${comment.end_line})`;
    }
    md += '\n\n';
    if (error) {
      md += `⚠️ GitHub could not post this as an inline comment: ${error}\n\n`;
    }
    md += comment.content || '';
    return md;
  }

  function buildSummaryBody(total, inline, summary, warnings) {
    let body = `🔍 **OpenCodeReview** found **${total}** issue(s) in this PR.`;
    if (total > 0) {
      body += `\n- ✅ ${inline} posted as inline comment(s)`;
      body += `\n- 📝 ${summary} posted as summary`;
    }
    if (warnings.length > 0) {
      body += `\n\n⚠️ ${warnings.length} warning(s) occurred during review.`;
    }
    return body;
  }

  function formatSummaryComments(summaryComments) {
    let body = '';
    for (const { comment } of summaryComments) {
      body += '\n\n---\n\n' + formatCommentMarkdown(comment);
    }
    return body;
  }

  function fencedBlock(content, language = '') {
    const text = String(content || '');
    const fence = safeFence(text);
    let block = fence + language + '\n' + text;
    if (!text.endsWith('\n')) block += '\n';
    return block + fence;
  }

  function safeFence(content) {
    const matches = String(content || '').match(/`+/g) || [];
    const maxTicks = matches.reduce((max, ticks) => Math.max(max, ticks.length), 0);
    return '`'.repeat(Math.max(3, maxTicks + 1));
  }

  function looksLikeOperationalFailure(message, stderr, result) {
    const blob = `${message || ''}\n${stderr || ''}\n${JSON.stringify(result || {})}`;
    if (/context deadline exceeded/i.test(blob)) return true;
    if (/LLM completion error/i.test(blob)) return true;
    if (/all \d+ file review\(s\) failed/i.test(blob)) return true;
    if (/check your LLM configuration and API key/i.test(message || '')) return true;
    if (result && result.status && result.status !== 'success' && !(result.comments || []).length) {
      return true;
    }
    return false;
  }

  function explainFailure(stderr, message) {
    const blob = `${stderr || ''}\n${message || ''}`;
    if (/context deadline exceeded/i.test(blob)) {
      return (
        'The LLM request timed out (`context deadline exceeded`). ' +
        'On local CPU this usually means OCR’s per-request HTTP timeout (default 300s) ' +
        'was too short — not a bad API key. The workflow should set `OCR_LLM_TIMEOUT` ' +
        '(e.g. 900) for local MAX.'
      );
    }
    if (/unknown config key/i.test(blob)) {
      return 'OCR rejected a config key. Check the job log for `unknown config key` (not an API-key issue).';
    }
    if (/check your LLM configuration and API key/i.test(blob)) {
      return (
        'OCR reported that all file reviews failed. Its stock message mentions an API key, ' +
        'but the real cause is usually in the log below (timeout, bad tool calls, unreachable LLM, etc.).'
      );
    }
    return null;
  }

  function formatErrorComment(stderr, message) {
    const explanation = explainFailure(stderr, message);
    let body = '⚠️ **OpenCodeReview** encountered an error';
    if (explanation) {
      body += `\n\n${explanation}`;
    }
    const detail = (stderr || message || '').trim();
    if (detail) {
      body += `\n\n${fencedBlock(detail)}`;
    }
    return body;
  }

  function logRateLimitQuota(response, tag) {
    try {
      const h = (response && response.headers) || {};
      const remaining = h['x-ratelimit-remaining'];
      if (remaining != null) {
        core.info(`[rate-limit] ${tag}: remaining=${remaining}`);
      }
      return remaining != null ? Number(remaining) : null;
    } catch (_) {
      return null;
    }
  }
};
