// indexer/fetch_csfd_tips.js
// Fetches the exact 12 TV tips of the day from csfd.cz/televize/ using native Node.js fetch

async function getCsfdTvTips() {
    try {
        const res = await fetch('https://www.csfd.cz/televize/', {
            headers: {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'cs,sk;q=0.9,en;q=0.8'
            }
        });
        if (!res.ok) {
            console.error(`HTTP error: ${res.status}`);
            return [];
        }
        const html = await res.text();
        const articles = html.match(/<article[\s\S]*?<\/article>/g) || [];
        const tips = [];

        for (const art of articles) {
            const titleMatch = art.match(/class="film-title-name"[^>]*>([^<]+)<\/a>/);
            const yearMatch = art.match(/<span class="info">\s*(\d{4})\s*<\/span>/);
            const stationMatch = art.match(/class="tv-label-btn"[^>]*>[\s\S]*?>\s*([^<]+)\s*<\/a>/);
            const timeMatch = art.match(/<strong>\s*(\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2})\s*<\/strong>/);
            const dayMatch = art.match(/<div class="tv-times">\s*<p>\s*([a-záčďéěíňóřšťúůýž]+)\s*<strong>/);
            const plotMatch = art.match(/<p class="p-tvtips-2row[^"]*">([\s\S]*?)<\/p>/);

            if (titleMatch) {
                const title = titleMatch[1].replace(/&amp;/g, '&').trim();
                const year = yearMatch ? parseInt(yearMatch[1], 10) : 0;
                let station = stationMatch ? stationMatch[1].replace(/&amp;/g, '&').trim() : '';
                const time = timeMatch ? timeMatch[1].trim() : '';
                const day = dayMatch ? dayMatch[1].trim() : 'dnes';
                let plot = plotMatch ? plotMatch[1].replace(/<[^>]+>/g, '').trim() : '';

                tips.push({
                    title,
                    year,
                    station,
                    time,
                    day,
                    plot
                });
            }
        }
        return tips;
    } catch (e) {
        console.error('Error fetching CSFD TV tips:', e);
        return [];
    }
}

getCsfdTvTips().then(tips => {
    console.log(JSON.stringify(tips, null, 2));
});
