// =============================================================================
// Vibe With Gary - Chat UI
// =============================================================================

const API_URL = window.location.hostname === 'localhost' || window.location.protocol === 'file:'
    ? 'https://api.vibewithgary.com'
    : 'https://api.vibewithgary.com';
const WS_URL = API_URL.replace('http', 'ws');

// State
let ws = null;
let sessionToken = null;
let currentSession = null;
let currentProject = null;
let sessions = [];
let projects = [];
let pendingApproval = null;
let broLevel = 100; // 0-100, default max bro!
let currentUser = null;
let hasDesktopAgent = false;
let currentSessionId = null;

// =============================================================================
// WebSocket Connection
// =============================================================================

let reconnectAttempts = 0;
const MAX_RECONNECT_ATTEMPTS = 10;
const RECONNECT_DELAY = 2000;

function connect(token) {
    sessionToken = token;
    updateStatus('connecting');

    ws = new WebSocket(`${WS_URL}/ws/client?token=${token}`);

    ws.onopen = () => {
        console.log('[Gary] WebSocket connected');
        updateStatus('connected');
        localStorage.setItem('gary_session_token', token);
        closeConnectModal();
        showApp();
        reconnectAttempts = 0; // Reset on successful connection
    };

    ws.onmessage = (event) => {
        handleMessage(JSON.parse(event.data));
    };

    ws.onclose = (event) => {
        console.log('[Gary] WebSocket closed:', event.code, event.reason);
        updateStatus('disconnected');
        ws = null;

        // Auto-reconnect if we have a token
        if (sessionToken && reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
            reconnectAttempts++;
            console.log(`[Gary] Reconnecting in ${RECONNECT_DELAY}ms (attempt ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})`);
            setTimeout(() => connect(sessionToken), RECONNECT_DELAY);
        }
    };

    ws.onerror = (error) => {
        console.log('[Gary] WebSocket error:', error);
        updateStatus('disconnected');
    };
}

function updateStatus(status) {
    const dot = document.getElementById('statusDot');
    const text = document.getElementById('statusText');
    const btn = document.getElementById('connectBtn');

    dot.className = 'status-dot ' + status;

    const statusMessages = {
        connected: 'Connected',
        connecting: 'Connecting...',
        disconnected: 'Disconnected'
    };
    text.textContent = statusMessages[status] || status;
    btn.style.display = status === 'connected' ? 'none' : 'block';
}

// =============================================================================
// Message Handling
// =============================================================================

function handleMessage(data) {
    switch (data.type) {
        case 'message':
            // Update session/project tracking
            const isNewSession = data.session_id && (!currentSession || currentSession.id !== data.session_id);

            if (data.session_id) {
                currentSession = { id: data.session_id, title: data.session_title || 'New Chat' };
            }
            if (data.project_id) {
                // Find full project object
                const proj = projects.find(p => p.id === data.project_id);
                if (proj) {
                    currentProject = proj;
                }
            }

            // Animate the response flowing in at the same speed as stories
            addMessage('assistant', data.content, '', true);

            // Refresh sidebar to show new chat after message is displayed
            if (isNewSession) {
                setTimeout(() => {
                    // Pass false to not clear messages - just refresh sidebar
                    selectProject(currentProject?.id || data.project_id, false);
                }, 100);
            }
            break;
        case 'thinking':
            showThinking(data.content);
            break;
        case 'tool_use':
            showToolUse(data.tool, data.input);
            break;
        case 'approval_required':
            showApprovalModal(data);
            break;
        case 'approval_request':
            showAgentApprovalModal(data);
            break;
        case 'file_change':
            showFileChange(data);
            break;
        case 'error':
            addMessage('assistant', `Error: ${data.content}`, 'error');
            break;
        case 'session_loaded':
            loadSessionHistory(data.messages);
            break;
        case 'code_output':
            showCodeOutput(data.output, data.exit_code, data.mode);
            break;
        case 'code_error':
            addMessage('assistant', `❌ Error: ${data.error}`, 'error');
            break;
    }
}

function showCodeOutput(output, exitCode, mode) {
    const modeIcon = mode === 'local' ? '🖥️' : '☁️';
    const modeLabel = mode === 'local' ? 'Local' : 'Virtual';
    const statusIcon = exitCode === 0 ? '✅' : '⚠️';

    const messagesEl = document.getElementById('messages');
    const outputEl = document.createElement('div');
    outputEl.className = 'message assistant';
    outputEl.innerHTML = `
        <div class="message-content">
            <div class="file-card">
                <div class="file-card-header">
                    <span class="icon">${modeIcon}</span>
                    <span>${modeLabel} Output ${statusIcon}</span>
                </div>
                <div class="file-card-body"><pre>${escapeHtml(output || '(no output)')}</pre></div>
            </div>
        </div>
    `;
    messagesEl.appendChild(outputEl);
    scrollToBottom();
}

let responseTypingInterval = null;

function addMessage(role, content, className = '', animateResponse = false) {
    hideWelcome();
    hideThinking();

    const messagesEl = document.getElementById('messages');
    const messageEl = document.createElement('div');
    messageEl.className = `message ${role} ${className}`;
    messageEl.id = `msg-${Date.now()}`;

    const contentEl = document.createElement('div');
    contentEl.className = 'message-content';

    // For assistant messages, animate the response flowing in
    if (role === 'assistant' && animateResponse && content.length > 0) {
        contentEl.innerHTML = '';
        messageEl.appendChild(contentEl);
        messagesEl.appendChild(messageEl);

        // Scroll to show start of response
        messageEl.scrollIntoView({ behavior: 'smooth', block: 'start' });

        // Type out the response
        typeResponse(content, contentEl);
    } else {
        contentEl.innerHTML = formatMessage(content);
        messageEl.appendChild(contentEl);
        messagesEl.appendChild(messageEl);
        scrollToBottom();
    }
}

function typeResponse(content, element) {
    // Clear any existing response typing
    if (responseTypingInterval) {
        clearTimeout(responseTypingInterval);
    }

    const formattedContent = formatMessage(content);
    let charIndex = 0;

    // We'll type the raw content and format as we go
    function typeNextChunk() {
        if (charIndex >= content.length) {
            // Done - ensure final formatted content is correct
            element.innerHTML = formattedContent;
            return;
        }

        // Type 1-2 characters at a time
        const charsToType = Math.random() > 0.7 ? 2 : 1;
        charIndex = Math.min(charIndex + charsToType, content.length);

        // Format and display what we have so far
        element.innerHTML = formatMessage(content.substring(0, charIndex));

        // Determine delay based on what we just typed (same speed as stories)
        const lastChar = content[charIndex - 1];
        let delay;

        if (lastChar === '.' || lastChar === '!' || lastChar === '?') {
            delay = 600 + Math.random() * 400;
        } else if (lastChar === ',') {
            delay = 300 + Math.random() * 200;
        } else if (lastChar === '-') {
            delay = 400 + Math.random() * 300;
        } else if (lastChar === ' ') {
            delay = 80 + Math.random() * 60;
        } else if (lastChar === '\n') {
            delay = 200 + Math.random() * 100;
        } else {
            delay = 50 + Math.random() * 40;
        }

        responseTypingInterval = setTimeout(typeNextChunk, delay);
    }

    typeNextChunk();
}

let codeBlockCounter = 0;

function formatMessage(content) {
    // Basic markdown-ish formatting
    // First, extract code blocks and replace with placeholders
    const codeBlocks = [];
    let processed = content.replace(/```(\w*)\n([\s\S]*?)```/g, (match, lang, code) => {
        const id = `code-block-${++codeBlockCounter}`;
        const escapedCode = code.replace(/</g, '&lt;').replace(/>/g, '&gt;');
        const langLabel = lang || 'code';
        const placeholder = `__CODEBLOCK_${codeBlocks.length}__`;
        codeBlocks.push(`<div class="code-block-wrapper">
            <button class="run-code-btn" onclick="runCodeBlock('${id}', '${lang || 'python'}')">Run</button>
            <pre><code id="${id}" data-lang="${langLabel}">${escapedCode}</code></pre>
        </div>`);
        return placeholder;
    });

    // Apply other formatting (these won't affect code blocks now)
    processed = processed
        // Inline code
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        // Bold
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        // Line breaks (only outside code blocks now)
        .replace(/\n/g, '<br>');

    // Restore code blocks
    codeBlocks.forEach((block, i) => {
        processed = processed.replace(`__CODEBLOCK_${i}__`, block);
    });

    return processed;
}

function runCodeBlock(blockId, lang) {
    const codeEl = document.getElementById(blockId);
    if (!codeEl) return;

    const code = codeEl.textContent;
    console.log('[Gary] Code block innerHTML:', codeEl.innerHTML);
    console.log('[Gary] Code block textContent:', JSON.stringify(code));
    console.log('[Gary] Code has newlines:', code.includes('\n'));

    if (!ws || ws.readyState !== WebSocket.OPEN) {
        alert('Not connected. Please refresh the page.');
        return;
    }

    // Add execution indicator
    const wrapper = codeEl.closest('.code-block-wrapper');
    let outputEl = wrapper.querySelector('.code-output');
    if (!outputEl) {
        outputEl = document.createElement('div');
        outputEl.className = 'code-output';
        wrapper.appendChild(outputEl);
    }
    outputEl.innerHTML = '<span class="running">Running...</span>';

    // Send run_code message
    ws.send(JSON.stringify({
        type: 'run_code',
        code: code,
        mode: hasDesktopAgent ? 'local' : 'virtual',
        language: lang,
        project_id: currentProject?.id,
        session_id: currentSessionId
    }));
}

// Gary's stories to tell while thinking
const GARY_STORIES = [
    {
        title: "The Great Coffee Incident",
        story: "So there I was, three days into a hackathon, running on nothing but cold pizza and Monster Energy. My buddy Jake had this 'brilliant' idea to hook up a coffee maker to our CI/CD pipeline. Every time a build failed, it would brew a shot of espresso. Seemed genius at first, right? Well, we had about 47 failing tests that night. By 3 AM, the coffee maker caught fire, set off the sprinklers, and we had to evacuate the entire building. The fire department showed up, and I had to explain to a very confused firefighter what 'continuous integration' meant while standing in a puddle of espresso. We still won third place though. The judges said they admired our 'commitment to automation.' Jake still owes me a new laptop..."
    },
    {
        title: "The Infinite Loop of Doom",
        story: "Okay so this one time, I was helping this startup debug their production server at like 2 AM. Classic startup vibes - ping pong table, beanbags, the whole deal. Anyway, they had this bug where every time someone ordered a pizza through their app, it would also order another pizza. And then THAT order would trigger another one. By the time they noticed, they had accidentally ordered 847 pizzas to their own office. The delivery guy thought it was a prank. Their CTO was literally crying. Turns out someone had copy-pasted a webhook handler and forgot to remove the retry logic. I fixed it in like 10 minutes but honestly I stayed for the pizza. We ate like kings that night, bro. The rest went to a homeless shelter so it worked out..."
    },
    {
        title: "The Regex That Broke Everything",
        story: "Bro, let me tell you about the time I thought I was a regex genius. I was working at this fintech company, feeling myself, wrote this absolute monster of a regular expression - like 200 characters long. It was supposed to validate phone numbers internationally. Tested it locally, worked perfectly. Pushed to prod on a Friday afternoon - first mistake, I know. Monday morning, the entire authentication system is down. Turns out my regex had catastrophic backtracking. Every login attempt was taking 45 seconds. Their AWS bill for that weekend? $12,000. Just on compute. For a regex. I learned two things that day: never deploy on Friday, and sometimes a simple string split is your best friend. My tech lead made me write 'I will not use greedy quantifiers' on the whiteboard 100 times..."
    },
    {
        title: "The Mysterious Production Bug",
        story: "This one still haunts me. I was debugging this issue where users in Australia couldn't upload profile pictures, but everyone else was fine. Spent three days on this. Checked timezones, CDN configs, image processing - nothing. Finally found it. Some genius had hardcoded a check that rejected any upload happening 'in the future' based on server time in California. Australia is literally tomorrow. So every upload from down under was getting rejected for being 'from the future.' The commit message that introduced this bug? 'Quick fix, no need for review.' I printed that out and taped it above my monitor as a reminder. The Australian users sent us a thank you card shaped like a kangaroo when we fixed it..."
    },
    {
        title: "The Accidental Email Blast",
        story: "Okay so I was testing an email notification system, right? Had this nice little script that would send test emails. Being responsible, I set up a test database with fake users. Except... I didn't connect to the test database. I connected to prod. With 2.3 million real users. And my test email subject line was 'TEST TEST IGNORE THIS - hey is anyone actually reading these?' followed by a message that said 'If you can see this, something has gone terribly wrong.' Within 30 seconds, our support inbox had 50,000 replies. Some users thought we got hacked. Others thought it was a game. One guy replied with his entire life story, including his divorce and his cat's medical history. We had to send an apology email, which I triple-checked went to the right database..."
    },
    {
        title: "The Printer That Knew Too Much",
        story: "At my old office, we had this ancient network printer that everyone hated. It would randomly print things at 3 AM. Creepy fortune cookie messages, random Wikipedia articles about the Ottoman Empire, once it printed 200 pages of the letter 'Q'. Everyone thought it was haunted. Turned out, it had been accidentally exposed to the internet and was on some botnet. But here's the wild part - before we fixed it, someone actually submitted the printer to a bug bounty program as a joke. They got $500 because the printer was technically leaking internal network information through its print queue. The printer got officially retired with a cake and everything. Someone made a LinkedIn profile for it. Last I checked it had 200 connections..."
    },
    {
        title: "The Database Migration Disaster",
        story: "Picture this: startup with 5 million users, and we need to migrate from MySQL to PostgreSQL. Simple, right? We planned for months. Had runbooks, rollback procedures, the works. D-day comes, everything goes smooth... until someone realizes we forgot to migrate the password salt table. So 5 million users wake up, try to log in, and none of their passwords work. But wait, it gets better. Our password reset email had a broken link because the URL format was different in the new system. So people couldn't reset passwords either. We ended up having to hire 20 temporary customer support people for two weeks just to manually verify and reset accounts. The founder still twitches when you say 'PostgreSQL' around him. We call it 'The Migration' - no other context needed..."
    },
    {
        title: "The Kubernetes Nightmare",
        story: "So I convinced my team that we absolutely NEEDED Kubernetes. We were running three containers total. Three. But I'd been watching all these DevOps talks, got hyped, wrote this beautiful helm chart. Took two months to set up properly. Finally deployed. Felt like a cloud native superhero. Then the bill came. We went from $200/month to $3,400/month. Turns out I'd configured auto-scaling a bit too aggressively. Our landing page was running on 47 pods. The 'About Us' page had its own dedicated cluster. My CTO asked if I was mining Bitcoin. I wasn't, but honestly that would've been more cost-effective. We went back to a single $5 droplet and everything still worked fine. I don't talk about my 'Kubernetes phase' anymore..."
    },
    {
        title: "The Git Catastrophe",
        story: "Fresh out of bootcamp, first real job. I'm feeling confident. Someone tells me to 'clean up the repo.' So I figure, why not delete all those old branches? Must be like 200 of them. I wrote a script to delete ALL branches except main. Pushed it. Except I didn't know about force push and I definitely didn't know the difference between local and remote branches. Deleted every single branch in the entire company repository. Including ones with active work. In the middle of a sprint. On demo day. My senior dev's face went completely white. Then he started laughing hysterically. Turns out he had a backup, but he made me sweat for like an hour first. Now I have 'git push --force' blocked in my terminal config..."
    },
    {
        title: "The Interview From Hell",
        story: "Early in my career, I interviewed at this super cool startup. Aced the phone screens, felt great. Showed up for the onsite, and they hand me a marker and say 'implement a red-black tree on this whiteboard.' Now, I'd literally reviewed red-black trees the night before. Knew them cold. But my marker was running out of ink. Every third line was invisible. I'm trying to explain the rotations while my diagram looks like a crime scene investigation board. I start sweating. Marker squeaks. The interviewer is squinting. I finally finish, and there's like 40% of a tree visible. Complete silence. Then the interviewer goes, 'Well, I appreciate that you didn't give up.' Got rejected but they gave me a free t-shirt, so not a total loss..."
    }
];

let storyInterval = null;
let storyDelayTimeout = null;
let currentStoryIndex = 0;
let pendingResponse = false;

function showThinking(content) {
    hideThinking();
    hideWelcome();

    const messagesEl = document.getElementById('messages');
    const thinkingEl = document.createElement('div');
    thinkingEl.id = 'thinkingIndicator';
    thinkingEl.className = 'message assistant';

    // Start with just a simple thinking indicator
    thinkingEl.innerHTML = `
        <div class="message-content">
            <div class="thinking-simple">
                <div class="typing-indicator">
                    <span></span><span></span><span></span>
                </div>
                <span class="thinking-text">Thinking...</span>
            </div>
            <div class="gary-story" id="garyStoryContainer" style="display: none;">
                <div class="story-text" id="storyText"></div>
            </div>
        </div>
    `;
    messagesEl.appendChild(thinkingEl);

    // Keep user's question in view - scroll to show the thinking indicator
    // but not so far that the question scrolls out
    thinkingEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

    // Only start story after 1 second delay
    storyDelayTimeout = setTimeout(() => {
        startStory();
    }, 1000);
}

function startStory() {
    const storyContainer = document.getElementById('garyStoryContainer');
    if (!storyContainer) return;

    // Pick a random story
    currentStoryIndex = Math.floor(Math.random() * GARY_STORIES.length);
    const story = GARY_STORIES[currentStoryIndex];

    storyContainer.style.display = 'block';

    // Start typing the story with natural cadence (half speed)
    typeStory(story.story);
}

function typeStory(text) {
    const storyTextEl = document.getElementById('storyText');
    if (!storyTextEl) return;

    let charIndex = 0;

    function typeNextChunk() {
        if (!document.getElementById('storyText')) {
            return;
        }

        if (charIndex >= text.length) {
            // Story finished naturally
            if (pendingResponse) {
                finishStoryAndTransition();
            }
            return;
        }

        // Check if we need to stop (response arrived)
        if (pendingResponse) {
            // Find the end of the current sentence
            const currentText = text.substring(0, charIndex);
            const lastChar = currentText[currentText.length - 1];

            // If we just finished a sentence, stop here
            if (lastChar === '.' || lastChar === '!' || lastChar === '?') {
                storyTextEl.textContent = currentText;
                finishStoryAndTransition();
                return;
            }

            // Otherwise keep typing until we hit a sentence end
            // But if we're mid-word, finish the word first
        }

        // Find the next word boundary to avoid cutting mid-word
        let charsToType = 1;
        const nextChar = text[charIndex];

        // If we're at a space, just type the space
        if (nextChar === ' ') {
            charsToType = 1;
        } else {
            // Find end of current word
            let wordEnd = charIndex;
            while (wordEnd < text.length && text[wordEnd] !== ' ' && text[wordEnd] !== '\n') {
                wordEnd++;
            }
            // Type the whole word at once (but limit to prevent huge jumps)
            charsToType = Math.min(wordEnd - charIndex, 12);
        }

        charIndex = Math.min(charIndex + charsToType, text.length);
        storyTextEl.textContent = text.substring(0, charIndex);

        // Determine delay based on what we just typed
        const lastChar = text[charIndex - 1];
        let delay;

        if (lastChar === '.' || lastChar === '!' || lastChar === '?') {
            // Long pause after sentences
            delay = 600 + Math.random() * 400;
        } else if (lastChar === ',') {
            // Medium pause after commas
            delay = 300 + Math.random() * 200;
        } else if (lastChar === '-') {
            // Dramatic pause
            delay = 400 + Math.random() * 300;
        } else if (lastChar === ' ') {
            // Small pause between words
            delay = 80 + Math.random() * 60;
        } else {
            // Just finished a word, small pause
            delay = 60 + Math.random() * 40;
        }

        storyInterval = setTimeout(typeNextChunk, delay);
    }

    typeNextChunk();
}

function hideThinking() {
    // Clear the story delay timeout
    if (storyDelayTimeout) {
        clearTimeout(storyDelayTimeout);
        storyDelayTimeout = null;
    }

    const el = document.getElementById('thinkingIndicator');
    if (!el) return;

    const storyText = el.querySelector('#storyText');

    // If a story is being told, let the current sentence finish
    if (storyText && storyText.textContent.length > 50 && storyInterval) {
        // Mark that we need to stop after sentence
        pendingResponse = true;
        // The typeStory function will check this and finish the sentence
    } else {
        // No story or very short - just remove
        if (storyInterval) {
            clearTimeout(storyInterval);
            storyInterval = null;
        }
        el.remove();
    }
}

function finishStoryAndTransition() {
    const el = document.getElementById('thinkingIndicator');
    if (!el) return;

    // Clear any remaining interval
    if (storyInterval) {
        clearTimeout(storyInterval);
        storyInterval = null;
    }

    const storyText = el.querySelector('#storyText');
    if (storyText && storyText.textContent.length > 50) {
        // Keep the story visible - just remove the thinking part
        const thinkingSimple = el.querySelector('.thinking-simple');
        if (thinkingSimple) {
            thinkingSimple.remove();
        }

        // Add transition phrase to the story
        const transitionPhrases = [
            "But anyway, back to what you asked...",
            "Alright, enough storytime - here's what I got...",
            "We'll finish that one later, here's your answer...",
            "To be continued... but first, your code:",
            "Anyway, where were we? Oh right -",
            "But that's a story for another time. Here you go:",
            "Haha, good times. Anyway, check this out:"
        ];
        const phrase = transitionPhrases[Math.floor(Math.random() * transitionPhrases.length)];

        // Add transition to the story container
        const storyContainer = el.querySelector('#garyStoryContainer');
        if (storyContainer) {
            const transitionEl = document.createElement('div');
            transitionEl.className = 'story-transition-inline';
            transitionEl.innerHTML = `<em>${phrase}</em>`;
            storyContainer.appendChild(transitionEl);
        }

        // Remove the id so it doesn't interfere with future thinking indicators
        el.removeAttribute('id');
        el.classList.add('story-complete');
    } else {
        el.remove();
    }

    pendingResponse = false;
}

function showToolUse(tool, input) {
    const messagesEl = document.getElementById('messages');
    const toolEl = document.createElement('div');
    toolEl.className = 'message assistant';

    let icon = '🔧';
    let label = tool;

    const toolInfo = {
        'read_file': { icon: '📄', label: 'Reading file' },
        'write_file': { icon: '✏️', label: 'Writing file' },
        'edit_file': { icon: '📝', label: 'Editing file' },
        'bash': { icon: '💻', label: 'Running command' },
        'search': { icon: '🔍', label: 'Searching' }
    };

    if (toolInfo[tool]) {
        icon = toolInfo[tool].icon;
        label = toolInfo[tool].label;
    }

    toolEl.innerHTML = `
        <div class="message-content">
            <div class="file-card">
                <div class="file-card-header">
                    <span class="icon">${icon}</span>
                    <span>${label}</span>
                </div>
                <div class="file-card-body">${escapeHtml(typeof input === 'string' ? input : JSON.stringify(input, null, 2))}</div>
            </div>
        </div>
    `;
    messagesEl.appendChild(toolEl);
    scrollToBottom();
}

function showFileChange(data) {
    const messagesEl = document.getElementById('messages');
    const changeEl = document.createElement('div');
    changeEl.className = 'message assistant';

    changeEl.innerHTML = `
        <div class="message-content">
            <div class="file-card">
                <div class="file-card-header">
                    <span class="icon">📄</span>
                    <span>${data.action} ${data.file}</span>
                </div>
                <div class="file-card-body">${formatDiff(data.diff || data.content)}</div>
            </div>
        </div>
    `;
    messagesEl.appendChild(changeEl);
    scrollToBottom();
}

function formatDiff(diff) {
    if (!diff) return '';
    return diff.split('\n').map(line => {
        if (line.startsWith('+')) {
            return `<div class="diff-add">${escapeHtml(line)}</div>`;
        } else if (line.startsWith('-')) {
            return `<div class="diff-remove">${escapeHtml(line)}</div>`;
        }
        return `<div>${escapeHtml(line)}</div>`;
    }).join('');
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function hideWelcome() {
    const welcome = document.getElementById('welcomeMessage');
    if (welcome) welcome.style.display = 'none';
}

function scrollToBottom() {
    const container = document.getElementById('messagesContainer');
    container.scrollTop = container.scrollHeight;
}

// =============================================================================
// Sending Messages
// =============================================================================

function sendMessage() {
    const input = document.getElementById('messageInput');
    const message = input.value.trim();

    if (!message || !ws || ws.readyState !== WebSocket.OPEN) return;

    // Add user message to UI
    addMessage('user', message);

    // Send to server with bro level and project
    ws.send(JSON.stringify({
        type: 'message',
        content: message,
        session_id: currentSession?.id,
        project_id: currentProject?.id,
        bro_level: broLevel
    }));

    // Clear input
    input.value = '';
    autoResize(input);
}

// =============================================================================
// Code Execution
// =============================================================================

function extractCodeFromMessages() {
    // Get all code blocks from the current conversation
    const messages = document.querySelectorAll('.message.assistant .message-content');
    const codeBlocks = [];

    messages.forEach(msg => {
        const codes = msg.querySelectorAll('pre code');
        codes.forEach(code => {
            codeBlocks.push(code.textContent);
        });
    });

    return codeBlocks;
}

function getLatestCode() {
    const codeBlocks = extractCodeFromMessages();
    if (codeBlocks.length === 0) {
        return null;
    }
    // Return the most recent code block
    return codeBlocks[codeBlocks.length - 1];
}

function detectOS() {
    const ua = navigator.userAgent.toLowerCase();
    if (ua.includes('win')) return 'windows';
    if (ua.includes('mac')) return 'mac';
    if (ua.includes('linux')) return 'linux';
    return 'unknown';
}

function showAgentInstallModal() {
    const os = detectOS();
    const osNames = { windows: 'Windows', mac: 'macOS', linux: 'Linux' };
    const osName = osNames[os] || 'your system';

    // Create install modal
    const modal = document.createElement('div');
    modal.className = 'modal active';
    modal.id = 'agentInstallModal';
    modal.innerHTML = `
        <div class="modal-content" style="max-width: 550px;">
            <h2>Run Code Locally</h2>
            <p>Execute code on your machine with the Gary Agent.</p>

            <div class="install-detected">
                <span class="os-icon">${os === 'mac' ? '🍎' : os === 'windows' ? '🪟' : '🐧'}</span>
                <span>Detected: <strong>${osName}</strong></span>
            </div>

            <div class="install-steps">
                <div class="install-step">
                    <span class="step-num">1</span>
                    <div class="step-content">
                        <strong>Download the agent</strong>
                        <code>curl -O https://raw.githubusercontent.com/jaaronleesanderson/vibewithgary/main/agent/gary_agent.py</code>
                    </div>
                </div>
                <div class="install-step">
                    <span class="step-num">2</span>
                    <div class="step-content">
                        <strong>Run the agent</strong>
                        <code>python3 gary_agent.py</code>
                    </div>
                </div>
                <div class="install-step">
                    <span class="step-num">3</span>
                    <div class="step-content">
                        <strong>Enter the pairing code shown in the agent</strong>
                    </div>
                </div>
            </div>

            <div class="pairing-section">
                <p>Already running the agent? Enter your pairing code:</p>
                <div class="pairing-form">
                    <input type="text" id="pairingCodeInput" placeholder="ABC123" class="pairing-input" maxlength="6" style="text-transform: uppercase;">
                    <button class="btn-primary" onclick="submitPairingCode()">Connect</button>
                </div>
                <p id="pairingError" class="pairing-error" style="display: none;"></p>
            </div>

            <div class="modal-actions">
                <button class="btn-secondary" onclick="closeAgentInstallModal()">Close</button>
                <button class="btn-primary" onclick="closeAgentInstallModal(); runCodeVirtually();">Use Cloud Instead</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
}

async function submitPairingCode() {
    const input = document.getElementById('pairingCodeInput');
    const errorEl = document.getElementById('pairingError');
    const code = input.value.toUpperCase().trim();

    if (code.length !== 6) {
        errorEl.textContent = 'Pairing code must be 6 characters';
        errorEl.style.display = 'block';
        return;
    }

    const token = localStorage.getItem('gary_session_token');
    if (!token) {
        errorEl.textContent = 'Please login with GitHub first';
        errorEl.style.display = 'block';
        return;
    }

    try {
        const response = await fetch(`${API_URL}/api/pair-agent`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ code })
        });

        if (response.ok) {
            hasDesktopAgent = true;
            closeAgentInstallModal();
            updateStatus('connected');
            alert('Agent connected! You can now run code locally.');
        } else {
            const err = await response.json();
            errorEl.textContent = err.detail || 'Invalid or expired pairing code';
            errorEl.style.display = 'block';
        }
    } catch (e) {
        errorEl.textContent = 'Connection error. Please try again.';
        errorEl.style.display = 'block';
    }
}

function closeAgentInstallModal() {
    const modal = document.getElementById('agentInstallModal');
    if (modal) modal.remove();
}

function runCodeLocally() {
    // Show install/connect modal for local environment
    showAgentInstallModal();
}

function runCodeVirtually() {
    // Show VM setup modal
    showVMSetupModal();
}

function showVMSetupModal() {
    const modal = document.createElement('div');
    modal.className = 'modal active';
    modal.id = 'vmSetupModal';
    modal.innerHTML = `
        <div class="modal-content" style="max-width: 500px;">
            <h2>Cloud Execution Ready</h2>
            <p>Run code in isolated cloud environments. Just click "Run" on any code block!</p>

            <div class="vm-options">
                <div class="vm-option selected">
                    <span class="vm-icon">🐍</span>
                    <div class="vm-details">
                        <strong>Python</strong>
                        <span>Python 3.11 with common packages</span>
                    </div>
                </div>
                <div class="vm-option selected">
                    <span class="vm-icon">💚</span>
                    <div class="vm-details">
                        <strong>JavaScript</strong>
                        <span>Node.js 20 with npm</span>
                    </div>
                </div>
                <div class="vm-option selected">
                    <span class="vm-icon">🐚</span>
                    <div class="vm-details">
                        <strong>Bash</strong>
                        <span>Shell scripts in Alpine Linux</span>
                    </div>
                </div>
            </div>

            <div class="vm-pricing">
                <p><strong>How it works:</strong></p>
                <ul style="margin: 8px 0; padding-left: 20px; color: var(--text-secondary);">
                    <li>Ask Gary for code, or paste your own</li>
                    <li>Click the "Run" button on any code block</li>
                    <li>Code runs in a secure cloud container</li>
                    <li>Results appear below the code</li>
                </ul>
                <p class="vm-note">30 second timeout per execution</p>
            </div>

            <div class="modal-actions">
                <button class="btn-primary" onclick="closeVMSetupModal()">Got it!</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
}

function closeVMSetupModal() {
    const modal = document.getElementById('vmSetupModal');
    if (modal) modal.remove();
}

function sendQuickAction(action) {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
        showConnectModal();
        return;
    }

    const input = document.getElementById('messageInput');
    input.value = action;
    sendMessage();
}

function handleKeyDown(event) {
    if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
    }
}

function autoResize(textarea) {
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(textarea.scrollHeight, 150) + 'px';
}

// =============================================================================
// Approval Flow
// =============================================================================

function showApprovalModal(data) {
    pendingApproval = data;

    document.getElementById('approvalAction').textContent = data.description;
    document.getElementById('approvalDetails').textContent = data.details || '';
    document.getElementById('approvalModal').classList.add('active');
}

function approveAction() {
    if (pendingApproval && ws) {
        ws.send(JSON.stringify({
            type: 'approval_response',
            approved: true,
            id: pendingApproval.id
        }));
    }
    closeApprovalModal();
}

function rejectAction() {
    if (pendingApproval && ws) {
        ws.send(JSON.stringify({
            type: 'approval_response',
            approved: false,
            id: pendingApproval.id
        }));
    }
    closeApprovalModal();
}

function closeApprovalModal() {
    document.getElementById('approvalModal').classList.remove('active');
    pendingApproval = null;
}

// =============================================================================
// Agent Approval Modal (for remote approvals from desktop agent)
// =============================================================================

let pendingAgentApproval = null;

function showAgentApprovalModal(data) {
    console.log('[Gary] showAgentApprovalModal called with:', data);
    pendingAgentApproval = data;
    const details = data.details || {};

    // Create modal if it doesn't exist
    let modal = document.getElementById('agentApprovalModal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'agentApprovalModal';
        modal.className = 'modal';
        document.body.appendChild(modal);
    }

    // Format the details based on operation type
    let detailsHtml = '';
    const op = details.operation;

    if (op === 'write_file' || op === 'edit_file' || op === 'delete_file') {
        detailsHtml = `<div class="approval-detail"><strong>Path:</strong> ${escapeHtml(details.path || '')}</div>`;
    }

    if (op === 'write_file' && details.preview) {
        detailsHtml += `
            <div class="approval-detail">
                <strong>Content:</strong> (${details.total_lines || 0} lines)
                <pre class="approval-preview">${escapeHtml(details.preview)}</pre>
            </div>`;
    }

    if (op === 'edit_file') {
        detailsHtml += `
            <div class="approval-detail">
                <strong>Replace:</strong>
                <pre class="approval-preview approval-remove">${escapeHtml(details.old_string || '')}</pre>
            </div>
            <div class="approval-detail">
                <strong>With:</strong>
                <pre class="approval-preview approval-add">${escapeHtml(details.new_string || '')}</pre>
            </div>`;
    }

    if (op === 'bash') {
        detailsHtml = `
            <div class="approval-detail"><strong>Command:</strong></div>
            <pre class="approval-preview">${escapeHtml(details.command || '')}</pre>
            <div class="approval-detail"><strong>Working Dir:</strong> ${escapeHtml(details.cwd || '')}</div>`;
    }

    if (op === 'execute') {
        detailsHtml = `
            <div class="approval-detail"><strong>Language:</strong> ${escapeHtml(details.language || '')}</div>
            <div class="approval-detail">
                <strong>Code:</strong> (${details.total_lines || 0} lines)
                <pre class="approval-preview">${escapeHtml(details.preview || '')}</pre>
            </div>`;
    }

    modal.innerHTML = `
        <div class="modal-content agent-approval-modal">
            <div class="modal-header approval-header">
                <span class="approval-icon">⚠️</span>
                <h3>Approval Required</h3>
            </div>
            <div class="modal-body">
                <div class="approval-operation">${escapeHtml(details.operation_name || details.operation || 'Unknown operation')}</div>
                ${detailsHtml}
            </div>
            <div class="modal-footer approval-actions">
                <button class="btn btn-danger" onclick="rejectAgentAction()">Deny</button>
                <button class="btn btn-secondary" onclick="trustAgentSession()">Trust Session</button>
                <button class="btn btn-primary" onclick="approveAgentAction()">Approve</button>
            </div>
        </div>
    `;

    modal.classList.add('active');

    // Play notification sound or vibrate
    if ('vibrate' in navigator) {
        navigator.vibrate(200);
    }
}

function approveAgentAction() {
    console.log('[Gary] approveAgentAction called');
    console.log('[Gary] pendingAgentApproval:', pendingAgentApproval);
    console.log('[Gary] ws:', ws);
    console.log('[Gary] ws.readyState:', ws ? ws.readyState : 'null');

    if (pendingAgentApproval && ws && ws.readyState === WebSocket.OPEN) {
        const msg = {
            type: 'approval_response',
            approval_id: pendingAgentApproval.approval_id,
            approved: true,
            trust: false
        };
        console.log('[Gary] Sending approval_response:', msg);
        ws.send(JSON.stringify(msg));
        console.log('[Gary] Sent!');
    } else {
        console.log('[Gary] Cannot send - missing pendingAgentApproval or ws not connected');
    }
    closeAgentApprovalModal();
}

function trustAgentSession() {
    console.log('[Gary] trustAgentSession called');
    if (pendingAgentApproval && ws && ws.readyState === WebSocket.OPEN) {
        const msg = {
            type: 'approval_response',
            approval_id: pendingAgentApproval.approval_id,
            approved: true,
            trust: true
        };
        console.log('[Gary] Sending trust approval_response:', msg);
        ws.send(JSON.stringify(msg));
    } else {
        console.log('[Gary] Cannot send trust - ws not connected');
    }
    closeAgentApprovalModal();
}

function rejectAgentAction() {
    console.log('[Gary] rejectAgentAction called');
    if (pendingAgentApproval && ws && ws.readyState === WebSocket.OPEN) {
        const msg = {
            type: 'approval_response',
            approval_id: pendingAgentApproval.approval_id,
            approved: false,
            trust: false
        };
        console.log('[Gary] Sending reject approval_response:', msg);
        ws.send(JSON.stringify(msg));
    } else {
        console.log('[Gary] Cannot send reject - ws not connected');
    }
    closeAgentApprovalModal();
}

function closeAgentApprovalModal() {
    const modal = document.getElementById('agentApprovalModal');
    if (modal) {
        modal.classList.remove('active');
    }
    pendingAgentApproval = null;
}

// =============================================================================
// Connect Modal
// =============================================================================

function showConnectModal() {
    document.getElementById('connectModal').classList.add('active');
    document.getElementById('pairingInput').focus();
}

function closeConnectModal() {
    document.getElementById('connectModal').classList.remove('active');
    document.getElementById('pairingInput').value = '';
}

async function connectWithCode() {
    const code = document.getElementById('pairingInput').value.trim().toUpperCase();
    if (code.length !== 6) {
        alert('Please enter a 6-character code');
        return;
    }

    const token = localStorage.getItem('gary_session_token');
    if (!token) {
        alert('Please login with GitHub first');
        loginWithGitHub();
        return;
    }

    try {
        const resp = await fetch(`${API_URL}/api/pair-agent`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ code })
        });
        const data = await resp.json();

        if (!resp.ok) {
            throw new Error(data.detail || 'Pairing failed');
        }

        // Successfully paired - update status and close modal
        hasDesktopAgent = true;
        closeConnectModal();
        updateStatus('connected');
        alert('Desktop agent connected! You can now run code locally.');
    } catch (e) {
        alert(e.message);
    }
}

async function loginWithGitHub() {
    // Redirect to GitHub OAuth
    window.location.href = `${API_URL}/auth/github`;
}

// =============================================================================
// Sessions
// =============================================================================

function newSession() {
    // Clear current session
    currentSession = null;

    // Clear messages and show welcome
    document.getElementById('messages').innerHTML = '';
    document.getElementById('welcomeMessage').style.display = 'block';

    // Tell server to start fresh session
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
            type: 'new_session',
            project_id: currentProject?.id
        }));
    }

    // Update sidebar
    renderSidebar();
}

function loadSessionHistory(messages) {
    document.getElementById('messages').innerHTML = '';
    hideWelcome();

    messages.forEach(msg => {
        addMessage(msg.role, msg.content);
    });
}

function renderSessions() {
    renderSidebar();
}

// Store chats per project for sidebar
const projectChatsCache = {};

async function loadAllProjectChats() {
    for (const project of projects) {
        if (!projectChatsCache[project.id]) {
            projectChatsCache[project.id] = await fetchProjectChats(project.id);
        }
    }
    renderSidebar();
}

function renderSidebar() {
    const list = document.getElementById('sessionsList');
    if (!list) return;

    if (projects.length === 0) {
        list.innerHTML = '<div class="no-projects">No projects yet</div>';
        return;
    }

    // Show all projects with their chats
    let html = '';

    for (const project of projects) {
        const isActive = currentProject?.id === project.id;
        const chats = isActive ? sessions : (projectChatsCache[project.id] || []);

        html += `
            <div class="sidebar-project ${isActive ? 'active' : ''}">
                <div class="project-header-item" onclick="selectProject('${project.id}')">
                    <span class="project-arrow">${isActive ? '▼' : '▶'}</span>
                    <span class="project-icon">📁</span>
                    <span class="project-name">${project.name}</span>
                </div>
        `;

        if (isActive) {
            html += '<div class="project-chats">';
            if (chats.length > 0) {
                html += chats.map(session => `
                    <div class="session-item ${session.id === currentSession?.id ? 'active' : ''}"
                         onclick="event.stopPropagation(); selectSession('${session.id}')">
                        <span class="chat-icon">💬</span>
                        <div class="chat-info">
                            <div class="title">${session.title || 'New Chat'}</div>
                            <div class="meta">${formatDate(session.updated_at)}</div>
                        </div>
                    </div>
                `).join('');
            } else {
                html += '<div class="no-chats">No chats yet</div>';
            }
            html += '</div>';
        }

        html += '</div>';
    }

    list.innerHTML = html;
}

async function selectSession(id) {
    const session = sessions.find(s => s.id === id);
    if (session) {
        currentSession = session;

        // Load session messages from API
        const token = localStorage.getItem('gary_session_token');
        try {
            const resp = await fetch(`${API_URL}/api/sessions/${id}`, {
                headers: { 'Authorization': `Bearer ${token}` }
            });
            if (resp.ok) {
                const data = await resp.json();
                // Clear and load messages
                document.getElementById('messages').innerHTML = '';
                hideWelcome();
                if (data.messages && data.messages.length > 0) {
                    data.messages.forEach(msg => {
                        addMessage(msg.role, msg.content);
                    });
                }
            }
        } catch (e) {
            console.error('Failed to load session:', e);
        }

        // Tell server we're on this session
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'set_session', session_id: id }));
        }

        renderSidebar();
    }
}

function formatDate(timestamp) {
    // Convert from seconds to milliseconds if needed
    const ts = timestamp < 10000000000 ? timestamp * 1000 : timestamp;
    const date = new Date(ts);
    const now = new Date();
    const diff = now - date;

    if (diff < 60000) return 'Just now';
    if (diff < 3600000) return `${Math.floor(diff/60000)}m ago`;
    if (diff < 86400000) return `${Math.floor(diff/3600000)}h ago`;
    return date.toLocaleDateString();
}

// =============================================================================
// Settings
// =============================================================================

function openSettings() {
    // TODO: Settings modal
    console.log('Settings');
}

// =============================================================================
// Projects
// =============================================================================

async function fetchProjects() {
    const token = localStorage.getItem('gary_session_token');
    if (!token) return;

    try {
        const resp = await fetch(`${API_URL}/api/projects`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        const data = await resp.json();
        projects = data.projects || [];
        renderProjectSelect();
        renderSidebar();

        // Auto-select first project if none selected
        if (!currentProject && projects.length > 0) {
            selectProject(projects[0].id);
        }
    } catch (e) {
        console.error('Failed to fetch projects:', e);
    }
}

async function createNewProject(name) {
    const token = localStorage.getItem('gary_session_token');
    if (!token) return null;

    try {
        const resp = await fetch(`${API_URL}/api/projects`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ name })
        });
        const data = await resp.json();
        await fetchProjects();
        return data;
    } catch (e) {
        console.error('Failed to create project:', e);
        return null;
    }
}

async function fetchProjectChats(projectId) {
    const token = localStorage.getItem('gary_session_token');
    if (!token) return [];

    try {
        const resp = await fetch(`${API_URL}/api/projects/${projectId}`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        const data = await resp.json();
        return data.chats || [];
    } catch (e) {
        console.error('Failed to fetch project chats:', e);
        return [];
    }
}

function renderProjectSelect() {
    const select = document.getElementById('projectSelect');
    if (!select) return;

    select.innerHTML = projects.map(p =>
        `<option value="${p.id}" ${currentProject?.id === p.id ? 'selected' : ''}>${p.name}</option>`
    ).join('') + '<option value="__new__">+ New Project</option>';
}

async function selectProject(projectId, clearMessages = true) {
    const project = projects.find(p => p.id === projectId);
    if (project) {
        currentProject = project;
        sessions = await fetchProjectChats(projectId);

        if (clearMessages) {
            currentSession = null;
            // Clear messages and show welcome for new project
            document.getElementById('messages').innerHTML = '';
            document.getElementById('welcomeMessage').style.display = 'block';

            // Tell server we switched projects
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({
                    type: 'set_project',
                    project_id: projectId
                }));
            }
        }

        renderProjectSelect();
        renderSidebar();
    }
}

function switchProject() {
    const select = document.getElementById('projectSelect');
    const projectId = select.value;

    if (projectId === '__new__') {
        const name = prompt('Project name:');
        if (name) {
            createNewProject(name).then(project => {
                if (project) {
                    // Clear messages for new project
                    document.getElementById('messages').innerHTML = '';
                    document.getElementById('welcomeMessage').style.display = 'block';
                    currentSession = null;
                    selectProject(project.project_id);
                }
            });
        } else {
            // Restore previous selection
            renderProjectSelect();
        }
        return;
    }

    if (projectId) {
        selectProject(projectId);
    }
}

// =============================================================================
// User Info
// =============================================================================

async function fetchUserInfo() {
    const token = localStorage.getItem('gary_session_token');
    if (!token) return;

    try {
        const resp = await fetch(`${API_URL}/api/me`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (resp.ok) {
            currentUser = await resp.json();
            updateUserDisplay();
        }
    } catch (e) {
        console.error('Failed to fetch user info:', e);
    }
}

async function checkAgentStatus() {
    const token = localStorage.getItem('gary_session_token');
    if (!token) return;

    try {
        const resp = await fetch(`${API_URL}/api/status`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (resp.ok) {
            const status = await resp.json();
            hasDesktopAgent = status.desktop_connected;
            console.log('[Gary] Agent status:', hasDesktopAgent ? 'connected' : 'not connected');
            updateAgentStatusUI();
        }
    } catch (e) {
        console.error('Failed to check agent status:', e);
    }
}

function updateAgentStatusUI() {
    const connectBtn = document.getElementById('connectBtn');
    const statusText = document.getElementById('statusText');

    if (hasDesktopAgent) {
        if (connectBtn) connectBtn.style.display = 'none';
        if (statusText) statusText.textContent = 'Local Agent Connected';
    }
}

function updateUserDisplay() {
    const avatar = document.getElementById('userAvatar');
    const userName = document.getElementById('userName');

    if (currentUser?.github_username) {
        if (avatar) {
            avatar.src = `https://github.com/${currentUser.github_username}.png`;
            avatar.alt = currentUser.github_username;
        }
        if (userName) {
            userName.textContent = currentUser.github_username;
        }
    }
}

// =============================================================================
// Bro Level
// =============================================================================

function updateBroLevel(value) {
    broLevel = parseInt(value);
    localStorage.setItem('gary_bro_level', broLevel);

    // Send to server if connected
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
            type: 'bro_level',
            level: broLevel
        }));
    }
}

function getBroLevel() {
    return broLevel;
}

// =============================================================================
// Device Detection
// =============================================================================

function isMobileDevice() {
    return /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent) ||
           (window.innerWidth <= 768);
}

function updateOnboardingForDevice() {
    const optionsContainer = document.querySelector('.onboarding-options');
    if (!optionsContainer) return;

    const isMobile = isMobileDevice();

    if (isMobile) {
        // Mobile options: Connect to computer OR Work remote (cloud)
        // Both require GitHub login first
        optionsContainer.innerHTML = `
            <div class="onboarding-option" onclick="showMobileConnectOption()">
                <div class="option-icon">🔗</div>
                <h3>Connect to Desktop</h3>
                <p>Link to your computer running the Gary agent</p>
            </div>
            <div class="onboarding-option" onclick="startCloudVMOption()">
                <div class="option-icon">☁️</div>
                <h3>Work Remote</h3>
                <p>Code from anywhere with GitHub repos</p>
            </div>
        `;
    } else {
        // Desktop options: Download agent OR Work on VM (cloud)
        optionsContainer.innerHTML = `
            <div class="onboarding-option" onclick="showDesktopAgentOption()">
                <div class="option-icon">🖥️</div>
                <h3>Desktop Agent</h3>
                <p>Run Gary on your machine with full access to your code</p>
            </div>
            <div class="onboarding-option" onclick="startCloudVMOption()">
                <div class="option-icon">☁️</div>
                <h3>Cloud VM</h3>
                <p>Work on a virtual machine with your GitHub repos</p>
            </div>
        `;
    }
}

function showDesktopAgentOption() {
    // For desktop agent, we need GitHub for backup/sync, then show pairing
    // First login with GitHub, then show pairing modal
    localStorage.setItem('gary_pending_action', 'desktop_agent');
    loginWithGitHub();
}

function showMobileConnectOption() {
    // For mobile connect to desktop, we need GitHub first, then show pairing
    localStorage.setItem('gary_pending_action', 'mobile_connect');
    loginWithGitHub();
}

function startChatOption() {
    // Just login and start chatting
    loginWithGitHub();
}

// =============================================================================
// Init
// =============================================================================

document.addEventListener('DOMContentLoaded', () => {
    // Update onboarding based on device type
    updateOnboardingForDevice();

    // Update onboarding on resize (for orientation changes)
    window.addEventListener('resize', updateOnboardingForDevice);
    // Load saved bro level
    const savedBroLevel = localStorage.getItem('gary_bro_level');
    if (savedBroLevel) {
        broLevel = parseInt(savedBroLevel);
    }

    // Check for GitHub OAuth callback
    const urlParams = new URLSearchParams(window.location.search);
    const tokenFromUrl = urlParams.get('token');
    const githubUser = urlParams.get('github_user');

    if (tokenFromUrl) {
        // Save token and clear URL params
        localStorage.setItem('gary_session_token', tokenFromUrl);
        if (githubUser) {
            localStorage.setItem('gary_github_user', githubUser);
        }
        // Clean up URL
        window.history.replaceState({}, document.title, window.location.pathname);

        // Check if there's a pending action from before OAuth
        const pendingAction = localStorage.getItem('gary_pending_action');
        console.log('[Gary] Pending action after OAuth:', pendingAction);
        localStorage.removeItem('gary_pending_action');

        if (pendingAction === 'desktop_agent' || pendingAction === 'mobile_connect') {
            // User was setting up desktop agent, show pairing modal
            console.log('[Gary] Desktop flow - showing pairing modal');
            showApp();
            connect(tokenFromUrl);
            fetchProjects();
            fetchUserInfo();
            checkAgentStatus();
            setTimeout(() => showConnectModal(), 500);
        } else {
            // Default: connect directly (Gary chat on server)
            console.log('[Gary] Connecting to Gary...');
            showApp();
            connect(tokenFromUrl);
            fetchProjects();
            fetchUserInfo();
            checkAgentStatus();
        }
    } else {
        // Check if already has a token (returning user)
        const savedToken = localStorage.getItem('gary_session_token');
        if (savedToken) {
            // Returning user - connect directly
            console.log('[Gary] Returning user - connecting...');
            showApp();
            connect(savedToken);
            fetchProjects();
            fetchUserInfo();
            checkAgentStatus();
        }
        // Otherwise show landing screen (default)
    }

    // Update UI with GitHub user if logged in
    const savedGithubUser = localStorage.getItem('gary_github_user');
    if (savedGithubUser) {
        updateGitHubUI(savedGithubUser);
    }

    // Pairing input - auto-submit on 6 digits
    document.getElementById('pairingInput').addEventListener('input', (e) => {
        if (e.target.value.length === 6) {
            connectWithCode();
        }
    });
});

function showOnboarding() {
    document.getElementById('landingScreen').classList.add('hidden');
    document.getElementById('onboardingScreen').classList.remove('hidden');
}

function showApp() {
    document.getElementById('landingScreen').classList.add('hidden');
    document.getElementById('onboardingScreen').classList.add('hidden');
    document.getElementById('app').classList.remove('hidden');

    // Set slider value
    const slider = document.getElementById('broSlider');
    if (slider) {
        slider.value = broLevel;
    }

    // Focus input
    setTimeout(() => {
        const input = document.getElementById('messageInput');
        if (input) input.focus();
    }, 100);
}

function updateGitHubUI(username) {
    // Update the GitHub login button to show username
    const githubBtn = document.querySelector('.github-login');
    if (githubBtn) {
        githubBtn.innerHTML = `<span class="icon">✓</span> ${username}`;
        githubBtn.onclick = null; // Disable click
    }
}

// =============================================================================
// Cloud VM Management
// =============================================================================

let cloudVM = null;

async function createCloudVM() {
    const token = localStorage.getItem('gary_session_token');
    if (!token) {
        console.error('No session token');
        return null;
    }

    try {
        const resp = await fetch(`${API_URL}/api/vm/create`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token}`
            }
        });

        const data = await resp.json();

        if (!resp.ok) {
            if (data.error && data.vm) {
                // Already have a VM
                cloudVM = data.vm;
                return data.vm;
            }
            throw new Error(data.detail || 'Failed to create VM');
        }

        cloudVM = data;
        return data;
    } catch (e) {
        console.error('Failed to create cloud VM:', e);
        return null;
    }
}

async function getCloudVMs() {
    const token = localStorage.getItem('gary_session_token');
    if (!token) return [];

    try {
        const resp = await fetch(`${API_URL}/api/vm`, {
            headers: {
                'Authorization': `Bearer ${token}`
            }
        });

        const data = await resp.json();
        return data.vms || [];
    } catch (e) {
        console.error('Failed to get VMs:', e);
        return [];
    }
}

async function destroyCloudVM(vmId) {
    const token = localStorage.getItem('gary_session_token');
    if (!token) return false;

    try {
        const resp = await fetch(`${API_URL}/api/vm/${vmId}`, {
            method: 'DELETE',
            headers: {
                'Authorization': `Bearer ${token}`
            }
        });

        return resp.ok;
    } catch (e) {
        console.error('Failed to destroy VM:', e);
        return false;
    }
}

async function startCloudSession() {
    console.log('[Gary] Starting cloud session...');

    // Show loading state
    updateStatus('connecting');

    const token = localStorage.getItem('gary_session_token');
    if (!token) {
        console.error('[Gary] No session token for cloud VM');
        updateStatus('disconnected');
        return;
    }

    // Check for existing VMs
    console.log('[Gary] Checking for existing VMs...');
    const vms = await getCloudVMs();
    let vm = vms.find(v => v.status === 'running');

    if (!vm) {
        // Create a new VM
        console.log('[Gary] Creating new cloud VM...');
        vm = await createCloudVM();
        if (!vm) {
            updateStatus('disconnected');
            alert('Failed to create cloud VM. Try again later.');
            return;
        }
        console.log('[Gary] VM created:', vm);
    } else {
        console.log('[Gary] Found existing VM:', vm);
    }

    cloudVM = vm;

    // Wait for the VM to connect to the relay (poll status endpoint)
    console.log('[Gary] Waiting for VM to connect...');
    let connected = false;
    for (let i = 0; i < 30; i++) {  // Wait up to 30 seconds
        try {
            const resp = await fetch(`${API_URL}/api/status`, {
                headers: { 'Authorization': `Bearer ${token}` }
            });
            const status = await resp.json();
            if (status.desktop_connected) {
                connected = true;
                console.log('[Gary] VM connected to relay!');
                break;
            }
        } catch (e) {
            console.log('[Gary] Status check error:', e);
        }
        await new Promise(r => setTimeout(r, 1000));
    }

    if (!connected) {
        console.error('[Gary] VM failed to connect in time');
        updateStatus('disconnected');
        alert('Cloud VM is taking too long to start. Please try again.');
        return;
    }

    // Now connect via WebSocket
    console.log('[Gary] Connecting client to relay...');
    connect(token);
}
