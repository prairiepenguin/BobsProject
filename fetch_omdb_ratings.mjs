import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';

const apiKey = process.env.OMDB_API_KEY || process.argv[2];
if (!apiKey) {
  console.error('Usage: node fetch_omdb_ratings.mjs <OMDB_API_KEY>');
  process.exit(1);
}

const cwd = process.cwd();
const tvdbPath = path.join(cwd, 'data', 'tvdb-194031-bobs-burgers-episodes.json');
const outputPath = path.join(cwd, 'data', 'omdb-bobs-burgers-ratings.json');
const payload = JSON.parse(await readFile(tvdbPath, 'utf8'));
const episodes = Array.isArray(payload.episodes) ? payload.episodes : [];
const seen = new Set();
const results = [];

function remoteIdMap(episode) {
  const ids = {};
  for (const remote of episode.remoteIds || []) {
    if (typeof remote === 'string') {
      const idx = remote.indexOf(':');
      if (idx > 0) ids[remote.slice(0, idx)] = remote.slice(idx + 1);
    } else if (remote && typeof remote === 'object') {
      const source = remote.sourceName || remote.source || remote.type || remote.name;
      const value = remote.id || remote.identifier || remote.url;
      if (source && value) ids[source] = String(value);
    }
  }
  return ids;
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

for (const episode of episodes) {
  const ids = remoteIdMap(episode);
  const imdbId = ids.IMDB || ids.IMDb || ids.imdb;
  if (!imdbId || seen.has(imdbId)) continue;
  seen.add(imdbId);

  try {
    const url = new URL('https://www.omdbapi.com/');
    url.searchParams.set('apikey', apiKey);
    url.searchParams.set('i', imdbId);
    url.searchParams.set('plot', 'short');
    url.searchParams.set('r', 'json');
    const response = await fetch(url);
    const omdb = await response.json();
    results.push({
      tvdbEpisodeId: episode.id,
      seasonNumber: episode.seasonNumber,
      episodeNumber: episode.number,
      title: episode.name,
      imdbId,
      response: omdb.Response,
      omdbTitle: omdb.Title ?? null,
      omdbYear: omdb.Year ?? null,
      imdbRating: omdb.imdbRating ?? null,
      imdbVotes: omdb.imdbVotes ?? null,
      metascore: omdb.Metascore ?? null,
      ratings: Array.isArray(omdb.Ratings) ? omdb.Ratings.map(r => ({ source: r.Source, value: r.Value })) : [],
      error: omdb.Error ?? null
    });
  } catch (error) {
    results.push({
      tvdbEpisodeId: episode.id,
      seasonNumber: episode.seasonNumber,
      episodeNumber: episode.number,
      title: episode.name,
      imdbId,
      response: 'False',
      omdbTitle: null,
      omdbYear: null,
      imdbRating: null,
      imdbVotes: null,
      metascore: null,
      ratings: [],
      error: error.message
    });
  }

  await sleep(120);
}

const output = {
  fetchedAt: new Date().toISOString(),
  source: {
    provider: 'OMDb API',
    tvdbPath,
    matchedBy: 'IMDb IDs from TVDB remoteIds'
  },
  totalTvdbEpisodes: episodes.length,
  requestedEpisodes: results.length,
  successfulEpisodes: results.filter(item => item.response === 'True').length,
  episodes: results
};

await writeFile(outputPath, JSON.stringify(output, null, 2), 'utf8');
console.log(`Wrote ${output.requestedEpisodes} records; ${output.successfulEpisodes} successful.`);
