import pytorch_lightning as pl
import torch
from torch import nn
from torch.nn import functional as F
from torch.optim import lr_scheduler
from math import exp

from modules import Encoder, PosEmbeds, SlotAttention, SlotAttentionBase, ClevrQuantizer, ClevrQuantizer2, CoordQuantizer
from utils import spatial_flatten, hungarian_huber_loss, average_precision_clevr


class QuantizedClassifier(pl.LightningModule):
    """
    Slot Attention based classifier for set prediction task
    """
    def __init__(self, resolution=(128, 128), num_slots=10, num_iters=3, in_channels=3, hidden_size=64, slot_size=64, lr=0.0004, base=True):
        super().__init__()
        self.resolution = resolution
        self.num_slots = num_slots
        self.num_iters = num_iters
        self.in_channels = in_channels
        self.hidden_size = hidden_size
        self.slot_size = slot_size

        self.encoder_cnn = Encoder(in_channels=self.in_channels, hidden_size=hidden_size)
        self.encoder_pos = PosEmbeds(hidden_size, (resolution[0] // 4, resolution[1] // 4))

        self.layer_norm = nn.LayerNorm(hidden_size)
        self.mlp = nn.Sequential(
            nn.Linear(hidden_size, hidden_size),
            nn.ReLU(),
            nn.Linear(hidden_size, slot_size)
        )
        if base:
            self.slot_attention = SlotAttentionBase(num_slots=num_slots, iters=num_iters, dim=slot_size, hidden_dim=slot_size*2)
        else:
            self.slot_attention = SlotAttention(num_slots=num_slots, iters=num_iters, dim=slot_size, hidden_dim=slot_size*2)

        self.mlp_classifier = nn.Sequential(
            nn.Linear(slot_size, hidden_size),
            nn.ReLU(),
            nn.Linear(hidden_size, 19),
            nn.Sigmoid()
        )
        self.mlp_coords = nn.Sequential(
            nn.Linear(slot_size, hidden_size),
            nn.ReLU(),
            nn.Linear(hidden_size, 3),
            nn.Sigmoid()
        )
        self.mlp_prop = nn.Sequential(
            nn.Linear(slot_size, hidden_size),
            nn.ReLU(),
            nn.Linear(hidden_size, 19 - 3),
            #nn.Sigmoid()
        )
        self.quantizer = ClevrQuantizer2()
        self.coord_quantizer = CoordQuantizer()
        
        self.thrs = [-1, 1, 0.5, 0.25, 0.125]
        self.smax = nn.Softmax(dim=-1)
        self.sigmoid = nn.Sigmoid()
        self.automatic_optimization = False
        self.t0 = 5.
        self.quantizer.temp = self.t0
        self.c = 0.00007
        self.istep = 0
        self.save_hyperparameters()
        self.base = base

    def forward(self, inputs):
        with torch.autograd.set_detect_anomaly(True):
            x = self.encoder_cnn(inputs)
            _, pos = self.encoder_pos(x)
            x = spatial_flatten(x)
            pos = spatial_flatten(pos)
            if self.base:
                x = x + pos
                x = x = self.mlp(self.layer_norm(x))
            x = self.slot_attention(x, pos, self.mlp, self.layer_norm)
            
            #coords, coord_entr = self.coord_quantizer(x)
            coords = x
            coords = self.mlp_coords(coords)
            
            #props, loss = self.quantizer(x)
            props = x
            props = self.mlp_prop(props)
            
            props[:, :, 0:2] = self.smax(props[:, :, 0:2].clone())
            props[:, :, 2:4] = self.smax(props[:, :, 2:4].clone())
            props[:, :, 4:7] = self.smax(props[:, :, 4:7].clone())
            props[:, :, 7:15] = self.smax(props[:, :, 7:15].clone())
            props[:, :, 15:] = self.sigmoid(props[:, :, 15:].clone()) 

            res = torch.cat([coords, props], dim=-1)
            loss, sim_loss, coord_entr = 0, 0, 0
        return res , loss, coord_entr#, sim_loss, com_loss

    def step(self, batch, batch_idx):
        images = batch['image']
        targets = batch['target']
        predictions, quant_loss, coord_entr = self(images)
        hung_loss = hungarian_huber_loss(predictions, targets)
        loss = hung_loss# + quant_loss

        metrics = {
            'loss': loss,
            # 'qunatizer loss': quant_loss,
            # 'inner sim loss': sim_loss, 
            # 'comitment loss': com_loss,
            'hungarian huber loss': hung_loss,
           # 'coord entropy': coord_entr
            }
        ap_metrics = {}
        if batch_idx == 1:
            ap_metrics = {
                f'ap thr={thr}': average_precision_clevr(
                    predictions.detach().cpu().numpy(), 
                    targets.detach().cpu().numpy(), 
                    thr
                    )
                for thr in self.thrs
            }

        return metrics, ap_metrics

    def training_step(self, batch, batch_idx):
        optimizer = self.optimizers()
        sch = self.lr_schedulers()

        metrics, ap_metrics = self.step(batch, batch_idx)
        self.log('training perf', metrics, on_step=False, on_epoch=True)
        if batch_idx == 1:
            self.log('train ap metrics', ap_metrics, on_step=True, on_epoch=False)

        optimizer.zero_grad()
        metrics['loss'].backward()
        optimizer.step()
        sch.step()
        self.log('lr', sch.get_last_lr()[0], on_step=False, on_epoch=True)
        self.log('temp', self.quantizer.temp, on_step=False, on_epoch=True)
        if self.quantizer.temp > 1.5:
            self.quantizer.temp = self.t0 * exp(self.istep *(-self.c))
            self.istep += 1

        return metrics['loss']

    def validation_step(self, batch, batch_idx):
        metrics, ap_metrics = self.step(batch, batch_idx)
        self.log('validation perf', metrics, on_step=False, on_epoch=True)
        if batch_idx == 1:
            self.log('val ap metrics', ap_metrics, on_step=True, on_epoch=False)
        return metrics['loss']

    def configure_optimizers(self):
        optimizer = torch.optim.AdamW(self.parameters(), lr=self.hparams["lr"])
        scheduler = lr_scheduler.OneCycleLR(optimizer, max_lr=self.hparams["lr"], total_steps=200000, pct_start=0.05)
        return [optimizer], [scheduler]
