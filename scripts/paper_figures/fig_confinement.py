import matplotlib; matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Polygon
# SCAI Lab Figure Guide v8, white theme
plt.rcParams.update({'font.family':'sans-serif','font.sans-serif':['Helvetica','Arial','Liberation Sans','DejaVu Sans'],
                     'font.size':8,'axes.labelsize':9,'xtick.labelsize':8,'ytick.labelsize':8,'legend.fontsize':8,
                     'mathtext.fontset':'custom','mathtext.it':'Liberation Sans:italic','mathtext.rm':'Liberation Sans',
                     'pdf.fonttype':42,'axes.linewidth':0.8,'axes.edgecolor':'#333333','text.color':'#111111',
                     'xtick.major.width':0.8,'axes.grid':False})
BUSY='#0A4C8A'; CLEAN='#6BAEE8'; BAND='#E5F0FC'  # SCAI ladder p5, p2, light fill
def freq(n): return 2404+2*n if n<=10 else 2428+2*(n-11)
busy=set(range(12,20))
sets=[('AFH free',None,'0–15%'),
      ('Confined',busy,'100%'),
      ('+ $k=2$',busy|{2,32},'80%'),
      ('+ $k=4$',busy|{0,4,30,34},'67%'),
      ('+ $k=8$',busy|{0,2,4,28,30,32,34,36},'47%')]
fig,ax=plt.subplots(figsize=(3.45,1.95))
y0=len(sets)
# Wi-Fi band (ch 6 occupied 16.25 MHz, nominal 20)
ax.add_patch(Rectangle((2437-8.125,-0.6),16.25,y0+0.05,color=BAND,lw=0,zorder=0))
ax.add_patch(Polygon([[2427,y0-0.25],[2429,y0+0.45],[2445,y0+0.45],[2447,y0-0.25]],closed=True,fc=BAND,ec=BUSY,lw=0.7))
ax.text(2437,y0+0.65,'Wi-Fi ch. 6, iperf3 UDP, 0–100% load',ha='center',va='bottom',color=BUSY,fontsize=8)
w=1.5
for i,(lab,used,note) in enumerate(sets):
    y=y0-1-i
    for n in range(37):
        f=freq(n)
        if used is None:
            inb = n in busy
            fc='white' if inb else CLEAN; ec=BUSY if inb else CLEAN; hatch='////' if inb else None
            ax.add_patch(Rectangle((f-w/2,y-0.3),w,0.6,fc=fc,ec=ec,lw=0.4,hatch=hatch))
        else:
            if n in used: fc=BUSY if n in busy else CLEAN; ec=fc
            else: fc='white'; ec='#D0D0D0'
            ax.add_patch(Rectangle((f-w/2,y-0.3),w,0.6,fc=fc,ec=ec,lw=0.4))
    ax.text(2400.5,y,lab,ha='right',va='center',fontsize=8)
    ax.text(2481.5,y,note,ha='left',va='center',fontsize=8,color='#666666')
ax.text(2481.5,y0-0.35,'Busy\nshare',ha='left',va='bottom',fontsize=8,color='#666666')
ax.set_xlim(2401,2481); ax.set_ylim(-0.7,y0+1.25)
ax.set_yticks([]); ax.set_xticks([2402,2420,2440,2460,2480])
for s in ['top','right','left']: ax.spines[s].set_visible(False)
ax.set_xlabel('Frequency (MHz)',labelpad=1)
from matplotlib.lines import Line2D
h=[Rectangle((0,0),1,1,fc=BUSY,ec=BUSY),Rectangle((0,0),1,1,fc=CLEAN,ec=CLEAN),Rectangle((0,0),1,1,fc='white',ec='#D0D0D0'),Rectangle((0,0),1,1,fc='white',ec=BUSY,hatch='////')]
ax.legend(h,['used, in band','used, clean','not used','removed under load'],
          loc='upper center',bbox_to_anchor=(0.45,-0.33),ncol=2,frameon=False,fontsize=7.5,handlelength=1.2,columnspacing=1.0)
fig.subplots_adjust(left=0.16,right=0.88,top=0.9,bottom=0.37)
import os
out=os.environ.get('FIG_OUT','.')
fig.savefig(f'{out}/fig_confinement.pdf'); fig.savefig(f'{out}/fig_confinement.png',dpi=220)
