# Prompt Log
I mainly used Claude Sonnet 5 on High Effort within the Amazon Kiro IDE for this project. I used several chats, separated by purpose, to modularize the parts of the project. For example, the UI and display questions and prompting were in one chat, while the citation graph generation was in another chat.


## Selected prompts 
### Moving from Command-Line to PyQt App
I now want to make a citation graph viewer using PyQt6 in python. Make a plan to do this. Ask me all clarifying questions before beginning. Write this is a new file and write neatly and don't write too complex if possible. The app should include these pages:

1. Search - There's a search bar at the top, which uses relevance_search to make a scrolling list of results. If you scroll to the bottom, a loading icon should be displayed and the next results should be loaded (if no errors & if present). When a result is clicked, it should switch to the viewer page and give the article (result) as a parameter / set it as a variable.
2. Viewer - Use the build citation_graph to asynchrously build the citation graph. Each paper should be a relatively big circle/node, with the authors' names on it (or first_author et. al.) and each reference should be an arrow (directed towards to the earlier, reference paper). As more papers are fetched, add more nodes. Allow the user to pan around and click on an individual node. This should open up a side window to view the paper in more detail. This should include the full title, authors, number of citations & references, and anticipate more fields added in the future.

### UI
- When dragging around, I don't want a circle to be selected if it happens to be the start of a click. I only want selection on mouse down + mouse up on the same element. Is there a way to do this automatically with Qt?
- Make a circle's outline bolded when selected.
- Add a link to the paper (perhaps DOI or open source link) on the side panel. Figure out what kind of extra information has to be requested in citation_graph.py's batch calls.
- From the search_page, only start searching if an entry is double-clicked or Enter is pressed on it. Currently, it starts if merely clicked, but this isn't what I'd expect from the interface.

### Graph Display
Potentially the biggest improvement: make the size of each circle dynamic, dependent on the number of citations it got. Make the minimum size 50 px diameter, and the maximum size 500 px diameter. Make the area (quadratic to diameter) be proportional to the reference count, up to the maximum. Make the original index paper in the center normalized to 300 px diameter.
Ask me any questions.
...
1. Use citationCount
For 2. and 3., k should be determined by the seed/index paper. Anything smaller or larger should be clamped.
4. Yes, anything <= 0 should be 50 px minimum.