const { useEffect, useRef } = React;

window.RainBackground = ({ speed = 1 }) => {
    const canvasRef = useRef(null);
    const mouse = useRef({ x: 0, y: 0 });

    const getSeasonConfig = () => {
        const month = new Date().getUTCMonth(); // 0 = Jan, 11 = Dec
        // CDT Seasons roughly map to standard months
        if ([11, 0, 1].includes(month)) return { name: 'winter', color: '200, 230, 255', count: 400, type: 'snow' };
        if ([2, 3, 4].includes(month)) return { name: 'spring', color: '255, 182, 193', count: 300, type: 'petal' };
        if ([5, 6, 7].includes(month)) return { name: 'summer', color: '239, 68, 68', count: 600, type: 'rain' };
        return { name: 'autumn', color: '255, 140, 0', count: 350, type: 'leaf' };
    };

    useEffect(() => {
        const canvas = canvasRef.current;
        const ctx = canvas.getContext('2d');
        const season = getSeasonConfig();
        let animationFrameId;

        const droplets = [];

        const resize = () => {
            canvas.width = window.innerWidth;
            canvas.height = window.innerHeight;
        };

        const createParticle = () => ({
            x: Math.random() * canvas.width,
            y: Math.random() * canvas.height,
            length: season.type === 'rain' ? Math.random() * 20 + 10 : Math.random() * 5 + 5,
            size: Math.random() * 2 + 1,
            speed: Math.random() * (season.type === 'snow' ? 2 : 10) + 3,
            opacity: Math.random() * 0.4 + 0.1,
            drift: Math.random() * 2 - 1,
            spin: Math.random() * 0.2
        });

        for (let i = 0; i < season.count; i++) droplets.push(createParticle());

        const handleMouseMove = (e) => {
            mouse.current.x = (e.clientX / window.innerWidth) - 0.5;
            mouse.current.y = (e.clientY / window.innerHeight) - 0.5;
        };

        const draw = () => {
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            const mouseOffsetX = mouse.current.x * 60;
            const tilt = mouse.current.x * (season.type === 'rain' ? 5 : 2);

            droplets.forEach(p => {
                ctx.beginPath();
                ctx.fillStyle = `rgba(${season.color}, ${p.opacity})`;
                ctx.strokeStyle = `rgba(${season.color}, ${p.opacity})`;
                ctx.lineWidth = p.size;

                if (season.type === 'rain') {
                    ctx.moveTo(p.x + mouseOffsetX, p.y);
                    ctx.lineTo(p.x + mouseOffsetX + tilt, p.y + p.length);
                    ctx.stroke();
                } else {
                    // Petal/Leaf/Snow circular or square drawing
                    ctx.arc(p.x + mouseOffsetX, p.y, p.size, 0, Math.PI * 2);
                    ctx.fill();
                }

                // Physics logic
                p.y += p.speed * speed;
                p.x += (p.drift + (mouse.current.x * 10)) * speed;

                if (p.y > canvas.height) {
                    p.y = -20;
                    p.x = Math.random() * canvas.width;
                }
                if (p.x > canvas.width) p.x = 0;
                if (p.x < 0) p.x = canvas.width;
            });

            animationFrameId = requestAnimationFrame(draw);
        };

        window.addEventListener('resize', resize);
        window.addEventListener('mousemove', handleMouseMove);
        resize();
        draw();

        return () => {
            window.removeEventListener('resize', resize);
            window.removeEventListener('mousemove', handleMouseMove);
            cancelAnimationFrame(animationFrameId);
        };
    }, [speed]);

    return <canvas ref={canvasRef} className="fixed top-0 left-0 w-full h-full pointer-events-none z-0" style={{ filter: 'blur(0.8px)' }} />;
};