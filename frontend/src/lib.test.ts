import { describe, expect, it } from 'vitest';
import {
  addDays, addMonths, diffContent, fieldLabel, fieldPaths, headline, isAdmin, isReviewer, jobState, monthGrid, parseContent,
  wallClock, zonedToInstant,
} from './lib';

describe('jobState', () => {
  it('folds queue and approval status into one state', () => {
    expect(jobState({ status: 'done', approval_status: 'pending' }).key).toBe('review');
    expect(jobState({ status: 'done', approval_status: 'approved' }).key).toBe('approved');
    expect(jobState({ status: 'done', approval_status: 'rejected' }).key).toBe('rejected');
    for (const s of ['queued', 'waiting', 'processing', 'retrying']) {
      expect(jobState({ status: s, approval_status: 'pending' })).toMatchObject({
        key: 'progress', active: true,
      });
    }
    for (const s of ['failed', 'dead', 'cancelled', 'expired', 'something-new']) {
      expect(jobState({ status: s, approval_status: 'pending' })).toMatchObject({
        key: 'stopped', active: false,
      });
    }
  });
});

describe('roles', () => {
  it('treats the owner as an admin and admins as reviewers', () => {
    expect(isAdmin('super_admin') && isAdmin('admin')).toBe(true);
    expect(isAdmin('manager')).toBe(false);
    expect(isReviewer('manager') && isReviewer('admin')).toBe(true);
    expect(isReviewer('user')).toBe(false);
  });
});

describe('parseContent and headline', () => {
  const draft = JSON.stringify({
    instagram: { caption: 'Golden hour' },
    content_summary: { main_message: 'A sunny product shot' },
    note: 'ignored scalar',
  });

  it('reads the JSON Gemini returns, even inside a code fence', () => {
    const fenced = parseContent('```json\n' + draft + '\n```');
    expect('structured' in fenced && Object.keys(fenced.structured)).toEqual([
      'instagram', 'content_summary',
    ]);
  });

  it('falls back to raw text', () => {
    expect(parseContent('just words')).toEqual({ raw: 'just words' });
    expect(parseContent('[1, 2]')).toEqual({ raw: '[1, 2]' });
  });

  it('prefers the summary, then captions, then the fallback', () => {
    expect(headline(draft, 'file.png')).toBe('A sunny product shot');
    expect(headline(JSON.stringify({ instagram: { caption: 'Hi' } }), 'f')).toBe('Hi');
    expect(headline('first line\nsecond', 'f')).toBe('first line');
    expect(headline(null, 'file.png')).toBe('file.png');
  });
});

describe('time helpers', () => {
  it('round-trips wall-clock time across a DST change', () => {
    // 2026-03-08 is the US spring-forward day.
    const instant = zonedToInstant('2026-03-08', '09:30', 'America/New_York');
    expect(instant.toISOString()).toBe('2026-03-08T13:30:00.000Z');
    expect(wallClock(instant, 'America/New_York')).toEqual({ date: '2026-03-08', time: '09:30' });
    expect(wallClock(instant, 'Asia/Karachi')).toEqual({ date: '2026-03-08', time: '18:30' });
  });

  it('does calendar arithmetic in whole days and months', () => {
    expect(addDays('2026-02-28', 1)).toBe('2026-03-01');
    expect(addMonths('2026-12', 1)).toBe('2027-01');
    const grid = monthGrid('2026-11');
    expect(grid).toHaveLength(42);
    expect(grid).toContain('2026-11-01');
    expect(grid).toContain('2026-11-30');
  });
});

describe('draft diffs and fields', () => {
  const v1 = JSON.stringify({
    instagram: { caption: 'Old caption', hashtags: ['#a', '#b'] },
    facebook: { post_copy: 'Same' },
  });
  const v2 = JSON.stringify({
    instagram: { caption: 'New caption', hashtags: ['#a', '#c'], hook: 'Added' },
    facebook: { post_copy: 'Same' },
  });

  it('lists only the fields that changed, including added ones', () => {
    expect(diffContent(v1, v2)).toEqual([
      { path: 'instagram.caption', before: 'Old caption', after: 'New caption' },
      { path: 'instagram.hashtags', before: '#a\n#b', after: '#a\n#c' },
      { path: 'instagram.hook', before: '', after: 'Added' },
    ]);
    expect(diffContent(v1, v1)).toEqual([]);
  });

  it('treats plain-text drafts as one field', () => {
    expect(diffContent('one', 'two')).toEqual([{ path: 'draft', before: 'one', after: 'two' }]);
  });

  it('names fields for people', () => {
    expect(fieldLabel('instagram.caption')).toBe('Instagram · Caption');
    expect(fieldLabel('tiktok.voiceover_script')).toBe('TikTok · Voiceover script');
    expect(fieldLabel('draft')).toBe('Draft');
    expect(fieldPaths(v1)).toEqual(['instagram.caption', 'instagram.hashtags', 'facebook.post_copy']);
    expect(fieldPaths('prose')).toEqual([]);
  });
});
